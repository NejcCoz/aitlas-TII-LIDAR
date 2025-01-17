import os
from pathlib import Path
from time import localtime, strftime

import geopandas as gpd
import glob
import pandas as pd
import rasterio
from rasterio.features import shapes
from shapely.geometry import box, shape

import grid_tools as gt
from adaf_vis import tiled_processing
from aitlas.models import FasterRCNN, HRNet
from adaf_utils import make_predictions_on_patches_object_detection, make_predictions_on_patches_segmentation
from vrt import build_vrt_from_list


def object_detection_vectors(path_to_patches, path_to_predictions):
    """Converts object detection bounding boxes from text to vector format.

    Parameters
    ----------
    path_to_patches : str or pathlib.Path
        Path to the directory with patches. Each prediction file (txt) corresponds to a patch file (tif), the function
        reads the geospatial metadata from the patch, to use them for geo-referencing the bounding box polygons.
        Adds two probability score and class label attributes to each polygon.
    path_to_predictions : str or pathlib.Path
        Path to directory with predictions, txt file, each line contains one predicted feature (multiple lines possible)

    Returns
    -------
    output_path : str
        Path to vector file.
    """
    # Use Path from pathlib
    path_to_patches = Path(path_to_patches)
    path_to_predictions = Path(path_to_predictions)
    # Prepare output path (GPKG file in the data folder)
    output_path = path_to_patches.parent / "object_detection.gpkg"

    appended_data = []
    for file in os.listdir(path_to_predictions):
        # Set path to individual PREDICTIONS FILE
        file_path = path_to_predictions / file

        # Only read files that are not empty
        if not os.stat(file_path).st_size == 0:
            # Find PATCH that belongs to the PREDICTIONS file
            patch_path = path_to_patches / (file[:-3] + "tif")

            # Arrays are indexed from the top-left corner, so we need minx and maxy
            with rasterio.open(patch_path) as src:
                crs = src.crs
                res = src.res[0]
                x_min = src.transform.c
                y_max = src.transform.f

            # Read predictions from TXT file
            data = pd.read_csv(file_path, sep=" ", header=None)
            data.columns = ["x0", "y0", "x1", "y1", "label", "score"]

            data.x0 = x_min + (res * data.x0)
            data.x1 = x_min + (res * data.x1)
            data.y0 = y_max - (res * data.y0)
            data.y1 = y_max - (res * data.y1)

            data["geometry"] = [box(*a) for a in zip(data.x0, data.y0, data.x1, data.y1)]
            data.drop(columns=["x0", "y0", "x1", "y1"], inplace=True)

            # data["ds"] = Path(file).stem.split("_")[0]
            data["ds"] = Path(file).stem

            appended_data.append(data)

    appended_data = pd.concat(appended_data)

    appended_data = gpd.GeoDataFrame(appended_data, columns=["label", "score", 'geometry'], crs=crs)

    appended_data = appended_data.dissolve(by='ds').explode(index_parts=False).reset_index(drop=False)

    appended_data.to_file(output_path.as_posix(), driver="GPKG")

    return output_path.as_posix()


def semantic_segmentation_vectors(path_to_predictions, threshold=0.5):
    """Converts semantic segmentation probability masks to polygons using a threshold.

    Parameters
    ----------
    path_to_predictions : str or pathlib.Path
        Path to the probability masks (tif format)
    threshold : float
        Probability threshold for predictions.

    Returns
    -------
    output_path : str
        Path to vector file.
    """
    labels = ["barrow", "ringfort", "enclosure"]  # TODO: Read this from model configuration

    # Prepare paths, use Path from pathlib
    path_to_predictions = Path(path_to_predictions)
    # Output path (GPKG file in the data folder)
    output_path = path_to_predictions.parent / "semantic_segmentation.gpkg"

    grids = []
    for label in labels:
        tif_list = list(path_to_predictions.glob(f"*{label}*.tif"))

        # file = tif_list[4]

        for file in tif_list:
            with rasterio.open(file) as src:
                prob_mask = src.read()
                transform = src.transform
                crs = src.crs

                prediction = prob_mask.copy()

                feature = prob_mask >= float(threshold)
                background = prob_mask < float(threshold)

                prediction[feature] = 1
                prediction[background] = 0

                # Outputs a list of (polygon, value) tuples
                output = list(shapes(prediction, transform=transform))

                # Find polygon covering valid data (value = 1) and transform to GDF friendly format
                poly = []
                for polygon, value in output:
                    if value == 1:
                        poly.append(shape(polygon))

            # Make Geodataframe
            if poly:
                grid = gpd.GeoDataFrame(poly, columns=['geometry'], crs=crs)
                grid = grid.dissolve().explode(ignore_index=True)
                grid["label"] = label
                # grid["ds"] = file.stem.split("_")[0]
                grid["ds"] = file.stem
                grids.append(grid)

    grids = gpd.GeoDataFrame(pd.concat(grids, ignore_index=True), crs=crs)

    grids = grids.dissolve(by='ds').explode(index_parts=False).reset_index(drop=False)

    grids.to_file(output_path.as_posix(), driver="GPKG")

    return output_path.as_posix()


def run_visualisations(dem_path, tile_size, save_dir, nr_processes=1):
    """Calculates visualisations from DEM and saves them into VRT (Geotiff) file.

    Uses RVT (see adaf_vis.py).

    dem_path:
        Can be any raster file (GeoTIFF and VRT supported.)
    tile_size:
        In pixels
    save_dir:
        Save directory
    nr_processes:
        Number of processes for parallel computing

    """
    # TODO: Probably good to create dict (JSOn) with all the paths that are created here?

    # Prepare paths
    in_file = Path(dem_path)
    ds_dir = in_file.parent

    # save_vis = save_dir / "vis"
    # save_vis.mkdir(parents=True, exist_ok=True)
    save_vis = save_dir  # TODO: figure out folder structure for outputs

    # === STEP 1 ===
    # We need polygon covering valid data
    valid_data_outline = gt.poly_from_valid(
        in_file.as_posix(),
        save_gpkg=save_vis  # directory where *_validDataMask.gpkg will be stored
    )

    # === STEP 2 ===
    # Create reference grid, filter it and save it to disk
    tiles_extents = gt.bounding_grid(
        in_file.as_posix(),
        tile_size,
        tag=False
    )
    refgrid_name = in_file.as_posix()[:-4] + "_refgrid.gpkg"
    tiles_extents = gt.filter_by_outline(
        tiles_extents,
        valid_data_outline,
        save_gpkg=True,
        save_path=refgrid_name
    )

    # === STEP 3 ===
    # Run visualizations
    print("Start RVT vis")
    out_path = tiled_processing(
        input_vrt_path=in_file.as_posix(),
        ext_list=tiles_extents,
        nr_processes=nr_processes,
        ll_dir=Path(save_vis)
    )

    # Remove reference grid and valid data mask files
    Path(valid_data_outline).unlink()
    Path(refgrid_name).unlink()

    return out_path["output_directory"]


def main_routine(dem_path, ml_type, model_path, vis_exist_ok):
    # Save results to parent folder of input file
    save_dir = Path(dem_path).parent

    # Create unique name for results
    time_stamp = strftime("%Y%m%d_%H%M%S", localtime())

    # vis_path is folder where visualizations are stored
    if vis_exist_ok:
        vis_path = dem_path
    else:
        # Determine nr_processes from available CPUs (leave two free)
        my_cpus = os.cpu_count() - 2
        if my_cpus < 1:
            my_cpus = 1

        # ## 1 ## Create visualisation
        tile_size_px = 1024  # TODO: Currently hardcoded, only tiling mode works with this tile size
        vis_path = run_visualisations(
            dem_path,
            tile_size_px,
            save_dir=save_dir.as_posix(),
            nr_processes=my_cpus
        )
    # Make sure it is a Path object!
    vis_path = Path(vis_path)

    # # ## 2 ## Create patches
    # patches_dir = create_patches(
    #     vis_path,
    #     patch_size_px,
    #     save_dir,
    #     nr_processes=nr_processes
    # )
    # shutil.rmtree(vis_path)

    if ml_type == "object detection":
        print("Running object detection")
        # ## 3 ## Run the model
        model_config = {
            "num_classes": 4,  # Number of classes in the dataset
            "learning_rate": 0.001,  # Learning rate for training
            "pretrained": True,  # Whether to use a pretrained model or not
            "use_cuda": False,  # Set to True if you want to use GPU acceleration
            "metrics": ["map"]  # Evaluation metrics to be used
        }
        model = FasterRCNN(model_config)
        model.prepare()
        model.load_model(model_path)
        print("Model successfully loaded.")
        predictions_dir = make_predictions_on_patches_object_detection(
            model=model,
            patches_folder=vis_path.as_posix()
        )

        # ## 4 ## Create map
        vector_path = object_detection_vectors(vis_path, predictions_dir)
        print("Created vector file", vector_path)

    elif ml_type == "segmentation":
        print("Running segmentation")
        # ## 3 ## Run the model
        model_config = {
            "num_classes": 3,  # Number of classes in the dataset
            "learning_rate": 0.0001,  # Learning rate for training
            "pretrained": True,  # Whether to use a pretrained model or not
            "use_cuda": False,  # Set to True if you want to use GPU acceleration
            "threshold": 0.5,
            "metrics": ["map"]  # Evaluation metrics to be used
        }
        model = HRNet(model_config)
        model.prepare()
        model.load_model(model_path)
        print("Model successfully loaded.")
        predictions_dir = make_predictions_on_patches_segmentation(
            model=model,
            patches_folder=vis_path.as_posix()
        )
        # ## 4 ## Create map
        vector_path = semantic_segmentation_vectors(predictions_dir)
        print("Created vector file", vector_path)
    else:
        raise Exception("Wrong ml_type: choose 'object detection' or 'segmentation'")

    # ## 5 ## Create VRT file for predictions
    for label in ["barrow", "ringfort", "enclosure"]:
        print("Creating vrt for", label)
        tif_list = glob.glob((Path(predictions_dir) / f"*{label}*.tif").as_posix())
        vrt_name = save_dir / (Path(predictions_dir).stem + f"_{label}.vrt")
        build_vrt_from_list(tif_list, vrt_name)

    print("\n--\nFINISHED!")

    return vector_path


if __name__ == "__main__":
    my_file = r"c:\Users\ncoz\GitHub\aitlas-TII-LIDAR\inference\data\archaeology1_TIN\135000_296000_archaeology1_TIN.tif"

    my_ml_type = "segmentation"  # "segmentation" or "object detection"

    my_tile_size_px = 1024

    # Specify the path to the model
    # OBJECT DETECTION:
    # my_model_path = r"c:\Users\ncoz\GitHub\aitlas-TII-LIDAR\inference\data\model_object_detection_BRE_12.tar"
    # SEGMENTATION:
    my_model_path = r"c:\Users\ncoz\GitHub\aitlas-TII-LIDAR\inference\models\model_semantic_segmentation_BRE_124.tar"

    rs = main_routine(my_file, my_ml_type, my_model_path, my_tile_size_px)

    # rs = object_detection_vectors(
    #     r"c:\Users\ncoz\GitHub\aitlas-TII-LIDAR\inference\data-147\slrm",
    #     r"c:\Users\ncoz\GitHub\aitlas-TII-LIDAR\inference\data-147\predictions_object_detection"
    # )

    # rs = run_visualisations(
    #     r"c:\Users\ncoz\GitHub\aitlas-TII-LIDAR\inference\data-small_debug\ISA-147_small.tif",
    #     1024,
    #     save_dir=r"c:\Users\ncoz\GitHub\aitlas-TII-LIDAR\inference\data-small_debug",
    #     nr_processes=6
    # )

    # rs = semantic_segmentation_vectors(
    #     r"C:\Users\ncoz\GitHub\aitlas-TII-LIDAR\inference\data-small_debug\predictions_segmentation", 0.5
    # )

    print(rs)
