import nibabel
from torch.utils.data import DistributedSampler
from torch.utils.tensorboard import SummaryWriter

import customTransforms
from SFCN import SFCNModelMONAI
from header_test import *
import monai
import nibabel

import torch.distributed as dist
from torch.nn.parallel import DistributedDataParallel as DDP
import torch.multiprocessing as mp

BATCH_SIZE = 8
N_WORKERS = 4
MAX_IMAGES = -1

def main():

    # train_images, val_images, test_images, mean_age, ages, get_age = read_data("/work/forkert_lab/erik/T1_warped",
    #                                                                            postfix=".nii.gz",
    #                                                                            max_entries=MAX_IMAGES)

    images, mean_age, ages, get_age = read_data("/work/forkert_lab/erik/MACAW/cf_images",
                                                                               postfix=".nii.gz",
                                                                               max_entries=MAX_IMAGES)

    # Add transforms to the dataset
    # transforms = Compose([monai.transforms.CenterSpatialCrop(roi_size=[150,150]),EnsureChannelFirst(), NormalizeIntensity()])
    transforms = Compose([customTransforms.Crop3D((150, 150, 150)), EnsureChannelFirst(), NormalizeIntensity()])

    # Define image dataset, data loader
    test_ds = ImageDataset(image_files=images, labels=ages, dtype=np.float32, transform=transforms,
                           reader="NibabelReader")

    test_loader = DataLoader(test_ds, shuffle=False, batch_size=BATCH_SIZE, num_workers=N_WORKERS,
                             pin_memory=torch.cuda.is_available())


    # Check if CUDA is available
    torch.cuda._lazy_init()
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(device)
    if device == "cuda":
        torch.cuda.empty_cache()
    if DEBUG:
        print("device: ", device)

    model=SFCNModelMONAI()
    model.load_state_dict(torch.load(f"/home/finn.vamosi/3Brain/models/end_model.pt"))

    MSELoss_fn = nn.MSELoss()
    MAELoss_fn = nn.L1Loss()

    # Testing
    print_title("Testing")
    model.eval()
    df = pd.DataFrame(columns=["Age", "Prediction", "ABSError", "ABSMEANError"])
    MSE_losses = []
    MAE_losses = []
    MAE_with_mean_losses = []

    with torch.no_grad():
        pbar3 = tqdm(test_loader)
        for data in pbar3:

            # Extract the input and the labels
            test_X, test_Y = data[0].to(device), data[1].to(device)
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
                df.loc[len(df)] = {"Age": test_Y[i].item(), "Prediction": ith_pred.item(),
                                   "ABSError": abs(test_Y[i].item() - ith_pred.item()),
                                   "ABSMEANError": abs(test_Y[i].item() - mean_age)}

    # End of testing
    print_title("End of Testing")
    print(f"MAE: {list_avg(MAE_losses)} MSE: {list_avg(MSE_losses)}")

    # Saving predictions into a .csv file
    df.to_csv("/home/finn.vamosi/3Brain/predictions.csv")

    if DEBUG:
        print_title("Testing Data")
        print(df.shape)
        print(df.head())


if __name__ == "__main__":

    if len(sys.argv) > 1:
        if (sys.argv[1] == '-d'):
            DEBUG = True
    else:
        DEBUG = False
    main()