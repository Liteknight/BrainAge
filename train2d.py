import nibabel
import torchvision
from torch.utils.tensorboard import SummaryWriter

import customTransforms
from header2d import *
import monai
import nibabel

import torch.distributed as dist
from torch.nn.parallel import DistributedDataParallel as DDP

BATCH_SIZE = 16
N_WORKERS = 4
N_EPOCHS = 50
MAX_IMAGES = -1
LR = 0.0001

def main():

    # Setup DDP:
    dist.init_process_group("nccl")
    rank = dist.get_rank()
    device = rank % torch.cuda.device_count()
    torch.manual_seed(1)
    torch.cuda.set_device(device)
    print(f"Starting rank={rank}, seed=1, world_size={dist.get_world_size()}.")

    # Reading the data and the denormalization function
    # images, mean_age, ages, get_age = read_data("data/91", postfix=".nii.gz", max_entries=MAX_IMAGES)
    images, mean_age, ages, get_age = read_data("/work/forkert_lab/erik/T1_cropped_slices/T1_cropped_slice_91", postfix=".tiff", max_entries=MAX_IMAGES)


    # Add transforms to the dataset
    # transforms = Compose([monai.transforms.CenterSpatialCrop(roi_size=[150,150]),EnsureChannelFirst(), NormalizeIntensity()])
    transforms = Compose([torchvision.transforms.CenterCrop(150),EnsureChannelFirst(), NormalizeIntensity()])

    # Define image dataset, data loader
    ds = ImageDataset(image_files=images, labels=ages, dtype=np.float32, transform=transforms,reader="ITKReader")
    # plt.imshow(ds[0][0][0][80])
    # plt.show()

    # Split the data into training and testing sets
    train_ds, val_ds, test_ds = torch.utils.data.random_split(ds, [.8, .1, .1])

    train_list_eid = [train_ds.dataset.image_files[x] for x in train_ds.indices]
    val_list_eid = [val_ds.dataset.image_files[x] for x in val_ds.indices]
    test_list_eid = [test_ds.dataset.image_files[x] for x in test_ds.indices]

    # print(train_list_eid)
    # print(val_list_eid)
    # print(test_list_eid)

    with open("split/train2d.txt", "w") as file:
        for name in train_list_eid:
            file.write("%s\n" % name)

    with open("split/validation2d.txt", "w") as file:
        for name in val_list_eid:
            file.write("%s\n" % name)

    with open("split/test2d.txt", "w") as file:
        for name in test_list_eid:
            file.write("%s\n" % name)

    train_loader = DataLoader(train_ds, shuffle=True, batch_size=BATCH_SIZE, num_workers=N_WORKERS, pin_memory=torch.cuda.is_available())
    val_loader = DataLoader(val_ds, shuffle=True, batch_size=BATCH_SIZE, num_workers=N_WORKERS, pin_memory=torch.cuda.is_available())
    test_loader = DataLoader(test_ds, shuffle=True, batch_size=BATCH_SIZE, num_workers=N_WORKERS, pin_memory=torch.cuda.is_available())
    
    if DEBUG:
        print_title("Image Properties")
        print(f"Max Tensor Value: {torch.max(ds[0][0])} Min Tensor Value: {torch.min(ds[0][0])}")
        print(f"Shape of the image {ds[0][0].shape}")
        print_title("Loading the data")
        print(f"length of ds: {len(ds)} ")
        print(f"Mean Age: {mean_age}")
        print_title("Data Splitting")
        print(f"Train: {len(train_ds)} Val: {len(val_ds)} Test: {len(test_ds)}")

    # Check if CUDA is available
    torch.cuda._lazy_init()
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(device)
    if device == "cuda":
        torch.cuda.empty_cache()
    if DEBUG:
        print("device: ", device)
    
    # Load the model
    model = DDP(SFCNModel().to(device), device_ids=[device])
    MSELoss_fn = nn.MSELoss()
    MAELoss_fn = nn.L1Loss()
    lr = LR
    opt = torch.optim.Adam(model.parameters(), lr)
    schdlr = torch.optim.lr_scheduler.StepLR(opt, step_size=N_EPOCHS//3, gamma=0.1)
    writer = SummaryWriter()

    # Training the model
    print_title("Training")
    min_MSE = torch.tensor(float("inf")).to(device)
    best_metric_epoch = -1
    ep_pbar = tqdm(range(N_EPOCHS))
    for epoch in ep_pbar:
        model.train()
        train_losses = []

        pbar = tqdm(train_loader)
        for data in pbar:

            # Extract the input and the labels
            train_X, train_Y = data[0].to(device) , data[1].to(device)
            train_Y = train_Y.type('torch.cuda.FloatTensor')

            # Zero the gradient
            opt.zero_grad()

            # Make a prediction
            pred = model(train_X)

            # Calculate the loss and backpropagate
            loss = MSELoss_fn(pred, train_Y)
            loss.backward()

            # Adjust the learning weights
            opt.step()

            # Calculate stats
            train_losses.append(loss.item())
            pbar.set_description(f"######## Training Loss: {loss.item():<.6f} ")
    
        # Validation
        model.eval()
        MSE_losses = []
        MAE_losses = []
        MAE_with_mean_losses = []

        with torch.no_grad():
            pbar2 = tqdm(val_loader)
            for data in pbar2:

                # Extract the input and the labels
                test_X, test_Y = data[0].to(device) , data[1].to(device)
                test_Y = test_Y.type('torch.cuda.FloatTensor')

                # Make a prediction
                #print("TEST: \n"+test_X)
                pred = model(test_X)

                # Calculate the losses
                MSE_loss = MSELoss_fn(pred, test_Y)
                MAE_loss = MAELoss_fn(pred, test_Y)
                MAE_with_mean_loss = MAE_with_mean_fn(mean_age, test_Y)

                MSE_losses.append(MSE_loss.item())
                MAE_losses.append(MAE_loss.item())
                MAE_with_mean_losses.append(MAE_with_mean_loss.item())

                # Display Loss
                pbar2.set_description(f"Epoch {epoch+1:<2} MSE Loss: {MSE_loss.item():<.4f} MAE Loss: {MAE_loss.item():<.4f} Last Predicted Age: {get_age(pred[-1].item()):<.4f} Last Actual Age: {get_age(test_Y[-1].item()):<.4f}")
        
        # Epoch over
        schdlr.step()

        # Save the model every 10th iteration if the loss is the lowest in this session
        if MSE_loss.item() < min_MSE.item() and epoch % 10 == 0:
            min_MSE = MSE_loss.detach()
            best_metric_epoch = epoch
            # torch.save(model.state_dict(), f'models/epoch_{epoch}_model.pt')
            torch.save(model.state_dict(), '/home/finn.vamosi/BrainAge/models/epoch_{epoch}_model.pt')

        writer.add_scalar(f"Training lr={LR}/MSE_train", list_avg(train_losses), epoch)
        writer.add_scalar(f"Testing lr={LR}/MAE_eval", list_avg(MAE_losses), epoch)
        writer.add_scalar(f"Testing lr={LR}/MSE_eval", list_avg(MSE_losses), epoch)
        writer.add_scalar(f"Testing lr={LR}/MAE_with_mean_eval", list_avg(MAE_with_mean_losses), epoch)

    # Training ended
    print_title("End of Training")
    print(f"best metric epoch: {best_metric_epoch}")
    print(f"best mertic (MSE): {min_MSE.item()}")

    writer.flush()

    # Saving the model
    # torch.save(model.state_dict(), 'models/end_model.pt')
    torch.save(model.state_dict(), '/home/finn.vamosi/BrainAge/models/end_model.pt')

    # Testing
    print_title("Testing")
    model.eval()
    df = pd.DataFrame(columns = ["Age", "Prediction", "ABSError", "ABSMEANError"])
    MSE_losses = []
    MAE_losses = []
    MAE_with_mean_losses = []

    with torch.no_grad():
        pbar3 = tqdm(test_loader)
        for data in pbar3:

            # Extract the input and the labels
            test_X, test_Y = data[0].to(device) , data[1].to(device)
            test_Y = test_Y.type('torch.cuda.FloatTensor')

            # Make a prediction
            pred = model(test_X)

            # Calculate the losses
            MSE_loss = MSELoss_fn(pred, test_Y)
            MAE_loss = MAELoss_fn(pred, test_Y)
            MAE_with_mean_loss = MAE_with_mean_fn(mean_age, test_Y)

            MSE_losses.append(MSE_loss.item())
            MAE_losses.append(MAE_loss.item())
            MAE_with_mean_losses.append(MAE_with_mean_loss.item())

            for i, ith_pred in enumerate(pred):
                df.loc[len(df)] = {"Age":test_Y[i].item(), "Prediction":ith_pred.item(), "ABSError": abs(test_Y[i].item() - ith_pred.item()), "ABSMEANError": abs(test_Y[i].item() - mean_age)}
    
    # End of testing
    print_title("End of Testing")
    print(f"MAE: {list_avg(MAE_losses)} MSE: {list_avg(MSE_losses)}")

    # Saving predictions into a .csv file
    df.to_csv("/home/finn.vamosi/BrainAge/predictions.csv")

    if DEBUG:
        print_title("Testing Data")
        print(df.shape)
        print(df.head())

if __name__ == "__main__":

    if len(sys.argv) > 1: 
        if(sys.argv[1] == '-d'):
            DEBUG = True
    else:
        DEBUG = False
    
    main()