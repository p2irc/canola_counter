
import os
import shutil
import glob

import logging
import time
import datetime
import math as m
import numpy as np
import tensorflow as tf
import uuid
from osgeo import gdal
from PIL import Image as PILImage
from PIL import ImageDraw as PILImageDraw
from shapely import Polygon, STRtree

from io_utils import json_io

from models.common import driver_utils, \
                          model_keys, \
                          box_utils, \
                          poly_utils

from image_wrapper import ImageWrapper
import emit


from models.yolov4.loss import YOLOv4Loss
from models.yolov4.yolov4 import YOLOv4, YOLOv4Tiny
import models.yolov4.data_load as data_load
from models.yolov4.encode import Decoder

MAX_EPOCHS_LIMIT = 400


def post_process_sample(detections, resize_ratio, patch_coords, config, region_bbox, apply_nms=True, score_threshold=0.5): #, round_scores=True):

    detections = np.array(detections)

    pred_xywh = detections[:, 0:4]
    pred_conf = detections[:, 4]
    pred_prob = detections[:, 5:]

    pred_boxes = (box_utils.swap_xy_tf(box_utils.convert_to_corners_tf(pred_xywh))).numpy()

    pred_boxes = np.stack([
            pred_boxes[:, 0] * resize_ratio[0],
            pred_boxes[:, 1] * resize_ratio[1],
            pred_boxes[:, 2] * resize_ratio[0],
            pred_boxes[:, 3] * resize_ratio[1]
    ], axis=-1)

    pred_classes = np.argmax(pred_prob, axis=-1)
    pred_scores = pred_conf * pred_prob[np.arange(len(pred_boxes)), pred_classes]

    score_mask = pred_scores > score_threshold
    pred_boxes, pred_scores, pred_classes = pred_boxes[score_mask], pred_scores[score_mask], pred_classes[score_mask]

    pred_boxes = box_utils.clip_boxes_np(pred_boxes, [0, 0, patch_coords[2] - patch_coords[0], patch_coords[3] - patch_coords[1]])
    if region_bbox is not None:
        pred_boxes = box_utils.clip_boxes_np(pred_boxes, [region_bbox[0] - patch_coords[0], region_bbox[1] - patch_coords[1],
                                                          region_bbox[2] - patch_coords[0], region_bbox[3] - patch_coords[1]])

    pred_boxes = np.rint(pred_boxes).astype(np.int32)
    pred_scores = pred_scores.astype(np.float32)
    pred_classes = pred_classes.astype(np.int32)

    valid_mask = np.logical_not(
                    np.logical_or(
                        (pred_boxes[:, 0] >= pred_boxes[:, 2]), 
                        (pred_boxes[:, 1] >= pred_boxes[:, 3])
                    )
    )

    pred_boxes = pred_boxes[valid_mask]
    pred_scores = pred_scores[valid_mask]
    pred_classes = pred_classes[valid_mask]



    if apply_nms:
        pred_boxes, pred_classes, pred_scores = box_utils.non_max_suppression_with_classes(
            pred_boxes,
            pred_classes,
            pred_scores,
            iou_thresh=config["inference"]["patch_nms_iou_thresh"])

    return pred_boxes, pred_scores, pred_classes




def create_default_config():


    config = {
        "model_name": "model_1",
        "model_uuid": str(uuid.uuid4()),
        "arch": {
            "model_type": "yolov4_tiny", #"yolov4_tiny", #"yolov4",
            "backbone_config": {
                "backbone_type": "csp_darknet53_tiny", #"csp_darknet53_tiny" #"csp_darknet53"
            },
            "neck_config": {
                "neck_type": "yolov4_tiny_deconv", #"yolov4_tiny_deconv" #"spp_pan"
            },
            "max_detections": 50,
            "input_image_shape": [416, 416, 3],
        },

        "training": {

            "learning_rate_schedule": {
                "schedule_type": "constant",
                "learning_rate": 0.0001
            },


            "data_augmentations": [

                # {
                #     "type": "CLAHE",
                #     "parameters": {
                #         "probability": 1.0,
                #     }
                # }
                {
                    "type": "flip_vertical", 
                    "parameters": {
                        "probability": 0.5
                    }
                },
                {
                    "type": "flip_horizontal", 
                    "parameters": {
                        "probability": 0.5
                    }
                },
                {
                    "type": "rotate_90", 
                    "parameters": {
                        "probability": 0.5
                    }
                },

                # {
                #     "type": "brightness_contrast",
                #     "parameters": {
                #         "probability": 1.0, 
                #         "brightness_limit": [-0.2, 0.2], 
                #         "contrast_limit": [-0.2, 0.2]
                #     }
                # },
                {
                    "type": "affine",
                    "parameters": {
                        "probability": 1.0, 
                        "scale": (0.85, 1.15), #1.0, 
                        "translate_percent": (-0.15, 0.15),
                        "rotate": 0, 
                        "shear": 0
                    }
                }
                # {
                #     "type": "shift_scale_rotate",
                #     "parameters": {
                #         "probability": 1.0, 
                #         "shift_limit": 0.15,
                #         "scale_limit": 0.0,
                #         "rotate_limit": 0.0
                #     }
                # }
            ],
            "batch_size": 16
        },
        "inference": {
            "batch_size": 64,
            "patch_nms_iou_thresh": 0.4,
            "image_nms_iou_thresh": 0.4,
            "score_thresh": 0.25,
        }
    }

    return config


def update_loss_record(loss_record, cur_loss):

    loss_vals = loss_record["training_loss_values"]
    loss_vals.append(cur_loss)

    return np.argmin(loss_vals) == (len(loss_vals) - 1)




def get_prediction_patches(request, patch_size, overlap_px, config):

    logger = logging.getLogger(__name__)

    incr = patch_size - overlap_px

    start_time = time.time()
    num_batches = 0
    num_patches = 0
    all_patch_coords = {}
    region_bboxes = {}
    for image_index, image_name in enumerate(request["image_names"]):
        all_patch_coords[image_name] = []
        region_bboxes[image_name] = []
        for region in request["regions"][image_index]:

            region_bbox = poly_utils.get_poly_bbox(region)

            region_height = region_bbox[2] - region_bbox[0]
            region_width = region_bbox[3] - region_bbox[1]

            w_covered = max(region_width - patch_size, 0)
            num_w_patches = m.ceil(w_covered / incr) + 1

            h_covered = max(region_height - patch_size, 0)
            num_h_patches = m.ceil(h_covered / incr) + 1

            min_coords = (np.array(list(np.ndindex(num_h_patches, num_w_patches))) * incr) + np.array([[region_bbox[0], region_bbox[1]]])
            patch_coords = np.hstack([min_coords, min_coords + patch_size])

            polys = np.stack([
                patch_coords[:, [0, 1]],
                patch_coords[:, [0, 3]],
                patch_coords[:, [2, 3]],
                patch_coords[:, [2, 1]]
            ], axis=1)

            s = STRtree([Polygon(x) for x in polys])
            result = s.query(Polygon(region), predicate="intersects")

            num_patches_for_region = len(result)

            num_patches += num_patches_for_region

            num_batches += m.ceil(num_patches_for_region / config["inference"]["batch_size"])

            all_patch_coords[image_name].append(patch_coords[result])
            region_bboxes[image_name].append(region_bbox)
            

    end_time = time.time()
    elapsed = round(end_time - start_time, 4)
    logger.info("Determined there are {} patches and {} batches in {} seconds.".format(
        num_patches, num_batches, elapsed))
    
    return all_patch_coords, region_bboxes, num_batches



def save_random_weights(num_classes, out_path):
    config = create_default_config()
    config["arch"]["class_map"] = {"class " + str(i): i for i in range(num_classes)}
    model_keys.add_general_keys(config)
    model_keys.add_specialized_keys(config)
    
    if config["arch"]["model_type"] == "yolov4":
        yolov4 = YOLOv4(config)
    elif config["arch"]["model_type"] == "yolov4_tiny":
        yolov4 = YOLOv4Tiny(config)

    input_shape = (config["inference"]["batch_size"], *(config["arch"]["input_image_shape"]))
    yolov4.build(input_shape=input_shape)

    yolov4.save_weights(filepath=out_path, save_format="h5")



def predict(job):

    start_time = time.time()

    logger = logging.getLogger(__name__)

    username = job["username"]
    farm_name =job["farm_name"]
    field_name = job["field_name"]
    mission_date = job["mission_date"]

    tf.keras.backend.clear_session()

    image_set_dir = os.path.join("usr", "data", username, "image_sets", farm_name, field_name, mission_date)

    model_dir = os.path.join(image_set_dir, "model")
    weights_dir = os.path.join(model_dir, "weights")


    metadata_path = os.path.join(image_set_dir, "metadata", "metadata.json")
    metadata = json_io.load_json(metadata_path)
    is_ortho = metadata["is_ortho"]


    status_path = os.path.join(model_dir, "status.json")
    status = json_io.load_json(status_path)


    config = create_default_config()
    config["arch"]["class_map"] = {x: i for i, x in enumerate(metadata["object_classes"])}
    model_keys.add_general_keys(config)
    model_keys.add_specialized_keys(config)

    if config["arch"]["model_type"] == "yolov4":
        yolov4 = YOLOv4(config)
    elif config["arch"]["model_type"] == "yolov4_tiny":
        yolov4 = YOLOv4Tiny(config)

    decoder = Decoder(config)

    predictions = {}

    patch_predictions = {}
    store_patch_predictions = False


    best_weights_path = os.path.join(weights_dir, "best_weights.h5")
    if not os.path.exists(best_weights_path):
        raise RuntimeError("Model weights could not be located.")

    input_shape = (config["inference"]["batch_size"], *(config["arch"]["input_image_shape"]))
    yolov4.build(input_shape=input_shape)
    yolov4.load_weights(best_weights_path, by_name=False)


    patch_size = status["patch_size"]
    patch_overlap_percent = 50
    overlap_px = int(m.floor(patch_size * (patch_overlap_percent / 100)))

    all_patch_data, region_bboxes, num_batches = get_prediction_patches(job, patch_size, overlap_px, config)
    percent_complete = 0
    batch_index = 0
    for image_index, image_name in enumerate(job["image_names"]):

        image_path = glob.glob(os.path.join(image_set_dir, "images", image_name + ".*"))[0]
        image = ImageWrapper(image_path)

        if is_ortho:
            ds = gdal.Open(image.image_path)
        else:
            image_array = image.load_image_array()

        for region_index, region in enumerate(job["regions"][image_index]):

            region_bbox = region_bboxes[image_name][region_index]
            region_patch_coords = all_patch_data[image_name][region_index]

            batch_patch_arrays = []
            batch_ratios = []
            batch_patch_coords = []

            for patch_index in range(len(region_patch_coords)):

                patch_coords = region_patch_coords[patch_index]
                patch_min_y = patch_coords[0]
                patch_min_x = patch_coords[1]
                patch_max_y = patch_coords[2]
                patch_max_x = patch_coords[3]


                max_content_y = min(patch_max_y, region_bbox[2])
                max_content_x = min(patch_max_x, region_bbox[3])

                patch_poly = [
                    [patch_min_y, patch_min_x],
                    [patch_min_y, patch_max_x],
                    [patch_max_y, patch_max_x],
                    [patch_max_y, patch_min_x]
                ]

                intersects, intersect_regions = poly_utils.get_intersection_polys(region, patch_poly)
                if intersects:

                    patch_array = np.zeros(shape=(patch_size, patch_size, 3), dtype=np.uint8)
                    if is_ortho:
                        image_array = ds.ReadAsArray(patch_min_x, patch_min_y, (max_content_x-patch_min_x), (max_content_y-patch_min_y))
                        image_array = np.transpose(image_array, (1, 2, 0))
                        patch_array[0:(max_content_y-patch_min_y), 0:(max_content_x-patch_min_x)] = image_array
                    else:
                        patch_array[0:(max_content_y-patch_min_y), 0:(max_content_x-patch_min_x)] = image_array[patch_min_y:max_content_y, patch_min_x:max_content_x]

                        tmp_img = PILImage.new("L", (patch_size, patch_size))

                    for intersect_region in intersect_regions:
                        polygon = []
                        for coord in intersect_region:
                            polygon.append((min(patch_size, max(0, round(coord[1] - patch_min_x))), 
                                            min(patch_size, max(0, round(coord[0] - patch_min_y)))))

                        if len(polygon) == 1:
                            PILImageDraw.Draw(tmp_img).point(polygon, fill=1)
                        else:
                            PILImageDraw.Draw(tmp_img).polygon(polygon, outline=1, fill=1)
                    mask = np.array(tmp_img) != 1
                    patch_array[mask] = [0, 0, 0]

                    patch_array = tf.cast(patch_array, dtype=tf.float32)
                    patch_ratio = np.array(patch_array.shape[:2]) / np.array(config["arch"]["input_image_shape"][:2])
                    patch_array = tf.image.resize(images=patch_array, size=config["arch"]["input_image_shape"][:2])

                    batch_patch_coords.append(patch_coords)
                    batch_patch_arrays.append(patch_array)
                    batch_ratios.append(patch_ratio)
                else:
                    logger.info("Unexpected non-intersection (patch: {}, region: {})".format(patch_coords, region))

                
                if len(batch_patch_arrays) == config["inference"]["batch_size"] or patch_index == len(region_patch_coords) - 1:
                    batch_patch_arrays = tf.stack(batch_patch_arrays, axis=0)
                    batch_size = batch_patch_arrays.shape[0]
                    
                    pred = yolov4(batch_patch_arrays, training=False)
                    detections = decoder(pred)

                    batch_pred_bbox = [tf.reshape(x, (batch_size, -1, tf.shape(x)[-1])) for x in detections]

                    batch_pred_bbox = tf.concat(batch_pred_bbox, axis=1)


                    for i in range(batch_size):

                        pred_bbox = batch_pred_bbox[i]
                        ratio = batch_ratios[i]
                        patch_coords = batch_patch_coords[i]


                        if store_patch_predictions:
                            pred_patch_abs_boxes, pred_patch_scores, _ = \
                                post_process_sample(pred_bbox, ratio, patch_coords, config, region_bbox, apply_nms=False, score_threshold=0.01) #config["inference"]["score_thresh"])
                            
                            if image_name not in patch_predictions:
                                patch_predictions[image_name] = {
                                    "patch_coords": [],
                                    "patch_boxes": [],
                                    "patch_scores": []
                                }
                            
                            patch_predictions[image_name]["patch_coords"].append(patch_coords)
                            patch_predictions[image_name]["patch_boxes"].append(pred_patch_abs_boxes.tolist())
                            patch_predictions[image_name]["patch_scores"].append(pred_patch_scores.tolist())

                        

                        pred_patch_abs_boxes, pred_patch_scores, pred_patch_classes = \
                                post_process_sample(pred_bbox, ratio, patch_coords, config, region_bbox, score_threshold=0.01) #config["inference"]["score_thresh"])



                        if image_name not in predictions:
                            predictions[image_name] = {
                                    "boxes": [],
                                    "scores": [],
                                    "classes": [],    
                            }


                        pred_image_abs_boxes, pred_image_scores, pred_image_classes = \
                            driver_utils.get_image_detections(pred_patch_abs_boxes, 
                                                            pred_patch_scores,
                                                            pred_patch_classes,
                                                            patch_coords, 
                                                            region_bbox,
                                                            trim=True)

                        predictions[image_name]["boxes"].extend(pred_image_abs_boxes.tolist())
                        predictions[image_name]["scores"].extend(pred_image_scores.tolist())
                        predictions[image_name]["classes"].extend(pred_image_classes.tolist())


                    batch_patch_arrays = []
                    batch_ratios = []
                    batch_patch_coords = []


                    batch_index += 1
                    prev_percent_complete = percent_complete
                    percent_complete = round((batch_index / num_batches) * 100)
                    if m.floor(percent_complete) > m.floor(prev_percent_complete):
                        emit.set_image_set_status(username, farm_name, field_name, mission_date, 
                                                    {"state_name": emit.PREDICTING, 
                                                    "progress": str(percent_complete) + "% Complete"}) 


    emit.set_image_set_status(username, farm_name, field_name, mission_date, 
                              {"state_name": emit.PREDICTING, 
                               "progress": "Saving Predictions"}) 

    start_nms_time = time.time()
    driver_utils.apply_nms_to_image_boxes(predictions, 
                                          iou_thresh=config["inference"]["image_nms_iou_thresh"])
    end_nms_time = time.time()
    elapsed_nms = str(datetime.timedelta(seconds=round(end_nms_time - start_nms_time)))
    logger.info("Finished running NMS. Time elapsed: {}".format(elapsed_nms))
    
    thresholded_predictions = {}
    for image_name in predictions.keys():
        scores_array = np.array(predictions[image_name]["scores"])

        inds = scores_array > 0.25

        thresholded_predictions[image_name] = {
            "boxes": (np.array(predictions[image_name]["boxes"])[inds]).tolist(),
            "scores": (scores_array[inds]).tolist(),
            "classes": (np.array(predictions[image_name]["classes"])[inds]).tolist(),
        }


    predictions_dir = os.path.join(model_dir, "prediction")
    for image_index, image_name in enumerate(job["image_names"]):
        image_predictions_path = os.path.join(predictions_dir, image_name + ".json")
        json_io.save_json(image_predictions_path, thresholded_predictions[image_name])


        # image_predictions_dir = os.path.join(predictions_dir, image_name)
        # os.makedirs(image_predictions_dir, exist_ok=True)

        # predictions_path = os.path.join(image_predictions_dir, "predictions.json")
        # new_predictions = {
        #     image_name: {
        #         "boxes": [],
        #         "scores": [],
        #         "classes": []
        #     }
        # }
        # if os.path.exists(predictions_path):
        #     existing_predictions = json_io.load_json(predictions_path)

        #     existing_boxes = np.array(existing_predictions[image_name]["boxes"])
        #     existing_box_centres = (existing_boxes[..., :2] + existing_boxes[..., 2:]) / 2.0
        #     existing_scores = np.array(existing_predictions[image_name]["scores"])
        #     existing_classes = np.array(existing_predictions[image_name]["classes"])
        #     mask = np.full(existing_boxes.shape[0], True)
        #     for region in job["regions"][image_index]:
        #         inds = poly_utils.get_contained_inds_for_points(existing_box_centres, [region])
        #         mask[inds] = False
        #     existing_boxes = existing_boxes[mask]
        #     existing_scores = existing_scores[mask]
        #     existing_classes = existing_classes[mask]
        #     new_predictions[image_name]["boxes"] = existing_boxes.tolist()
        #     new_predictions[image_name]["scores"] = existing_scores.tolist()
        #     new_predictions[image_name]["classes"] = existing_classes.tolist()

        # new_predictions[image_name]["boxes"].extend(thresholded_predictions[image_name]["boxes"])
        # new_predictions[image_name]["scores"].extend(thresholded_predictions[image_name]["scores"])
        # new_predictions[image_name]["classes"].extend(thresholded_predictions[image_name]["classes"])

        # json_io.save_json(predictions_path, new_predictions)

    if job["save_result"]:
        result_dir = os.path.join(model_dir, "results", "available", job["result_uuid"])
        if not os.path.exists(result_dir):
            os.makedirs(result_dir)
        
        full_predictions_path = os.path.join(result_dir, "full_predictions.json")
        json_io.save_json(full_predictions_path, predictions)

        image_set_predictions_dir = os.path.join(model_dir, "prediction")
        result_predictions_dir = os.path.join(result_dir, "prediction")
        os.makedirs(result_predictions_dir)
        for image_index, image_name in enumerate(job["image_names"]):
            image_set_image_predictions_path = os.path.join(image_set_predictions_dir, image_name + ".json")
            result_image_predictions_path = os.path.join(result_predictions_dir, image_name + ".json")
            shutil.copyfile(image_set_image_predictions_path, result_image_predictions_path)

        
        # predictions_path = os.path.join(results_dir, "predictions.json")
        # json_io.save_json(predictions_path, thresholded_predictions)





    if store_patch_predictions:
        patch_predictions_path = os.path.join(result_dir, "patch_predictions.json")
        json_io.save_json(patch_predictions_path, patch_predictions)



    end_time = time.time()
    elapsed = str(datetime.timedelta(seconds=round(end_time - start_time)))
    logger.info("Finished predicting. Time elapsed: {}".format(elapsed))

    return







def train(job):
    
    logger = logging.getLogger(__name__)

    model_creator = job["model_creator"]
    model_name = job["model_name"]
    
    tf.keras.backend.clear_session()

    model_creator_dir = os.path.join("usr", "data", model_creator)
    models_dir = os.path.join(model_creator_dir, "models")
    pending_dir = os.path.join(models_dir, "pending")
    baseline_pending_dir = os.path.join(pending_dir, model_name)


    weights_dir = os.path.join(baseline_pending_dir, "model", "weights")
    training_dir = os.path.join(baseline_pending_dir, "model", "training")

    training_tf_record_paths = [os.path.join(training_dir, "training-patches-record.tfrec")]

    
    log = json_io.load_json(os.path.join(baseline_pending_dir, "log.json"))

    config = create_default_config()
    config["arch"]["class_map"] = {x: i for i, x in enumerate(log["object_classes"])}
    model_keys.add_general_keys(config)
    model_keys.add_specialized_keys(config)

    config["training"]["active"] = {}
    for k in config["training"]:
        config["training"]["active"][k] = config["training"][k]
    
    train_data_loader = data_load.TrainDataLoader(training_tf_record_paths, config, shuffle=True, augment=True)
    train_dataset, num_train_patches = train_data_loader.create_batched_dataset()

    if job["training_regime"] == "train_val_split":
        validation_tf_record_paths = [os.path.join(training_dir, "validation-patches-record.tfrec")]
        val_data_loader = data_load.TrainDataLoader(validation_tf_record_paths, config, shuffle=False, augment=False)
        val_dataset, num_val_patches = val_data_loader.create_batched_dataset()
        val_loss_metric = tf.metrics.Mean()


    logger.info("Building model...")


    if config["arch"]["model_type"] == "yolov4":
        yolov4 = YOLOv4(config)
    elif config["arch"]["model_type"] == "yolov4_tiny":
        yolov4 = YOLOv4Tiny(config)

    loss_fn = YOLOv4Loss(config)


    input_shape = (config["training"]["active"]["batch_size"], *(train_data_loader.get_model_input_shape()))
    yolov4.build(input_shape=input_shape)

    logger.info("Model built.")


    cur_weights_path = os.path.join(weights_dir, "cur_weights.h5")
    best_weights_path = os.path.join(weights_dir, "best_weights.h5")
    if os.path.exists(cur_weights_path):
        logger.info("Loading weights...")
        yolov4.load_weights(cur_weights_path, by_name=False)
        logger.info("Weights loaded.")
    else:
        logger.info("No initial weights found.")


    optimizer = tf.optimizers.Adam()
    train_loss_metric = tf.metrics.Mean()


    @tf.function
    def train_step(batch_images, batch_labels):
        with tf.GradientTape() as tape:
            conv = yolov4(batch_images, training=True)
            loss_value = loss_fn(batch_labels, conv)
            loss_value += sum(yolov4.losses)

        gradients = tape.gradient(target=loss_value, sources=yolov4.trainable_variables)
        optimizer.apply_gradients(grads_and_vars=zip(gradients, yolov4.trainable_variables))
        train_loss_metric.update_state(values=loss_value)


    if job["training_regime"] == "fixed_num_epochs":
        logger.info("{} ('{}'): Starting to train with {} training patches.".format(
            config["arch"]["model_type"], config["model_name"], num_train_patches
        ))
    else:
        logger.info("{} ('{}'): Starting to train with {} training patches and {} validation patches.".format(
            config["arch"]["model_type"], config["model_name"], num_train_patches, num_val_patches
        ))

    while True:

        loss_record_path = os.path.join(training_dir, "loss_record.json")
        loss_record = json_io.load_json(loss_record_path)


        if job["training_regime"] == "fixed_num_epochs":

            num_epochs_trained = len(loss_record["train"]) - 1
            logger.info("{} / {} epochs completed.".format(num_epochs_trained, job["num_epochs"]))

            if num_epochs_trained >= job["num_epochs"]:
                logger.info("Finished training!")
                shutil.copyfile(best_weights_path, cur_weights_path)
                return

            
        else:

            epochs_since_improvement = get_epochs_since_val_loss_improvement(loss_record)
            logger.info("Epochs since validation loss improvement: {}".format(epochs_since_improvement))

            num_epochs_trained = len(loss_record["train"]) - 1
            if epochs_since_improvement >= job["improvement_tolerance"] or num_epochs_trained == MAX_EPOCHS_LIMIT:
                logger.info("Finished training! Trained for {} epochs in total.".format(num_epochs_trained))
                shutil.copyfile(best_weights_path, cur_weights_path)
                return



        for batch_data in train_dataset:

            optimizer.lr.assign(config["training"]["active"]["learning_rate_schedule"]["learning_rate"])

            batch_images, batch_labels = train_data_loader.read_batch_data(batch_data)

            train_step(batch_images, batch_labels)
            if np.isnan(train_loss_metric.result()):
                raise RuntimeError("NaN loss has occurred.")
            


        cur_train_loss = float(train_loss_metric.result())
        loss_record["train"].append(cur_train_loss)
        train_loss_metric.reset_states()



        if job["training_regime"] == "train_val_split":

            for batch_data in val_dataset:

                batch_images, batch_labels = val_data_loader.read_batch_data(batch_data)
                conv = yolov4(batch_images, training=False)
                loss_value = loss_fn(batch_labels, conv)
                loss_value += sum(yolov4.losses)

                val_loss_metric.update_state(values=loss_value)

                if np.isnan(val_loss_metric.result()):
                    raise RuntimeError("NaN loss has occurred.")
                

            cur_val_loss = float(val_loss_metric.result())
            loss_record["val"].append(cur_val_loss)
            val_loss_metric.reset_states()


        yolov4.save_weights(filepath=cur_weights_path, save_format="h5")
        if cur_epoch_is_best(loss_record, job):
            yolov4.save_weights(filepath=best_weights_path, save_format="h5")


        json_io.save_json(loss_record_path, loss_record)



def get_epochs_since_substantial_improvement(loss_record):

    vals = loss_record["training_loss_values"]
    if len(vals) <= 1:
        return 0
    
    SUBSTANTIAL_IMPROVEMENT_THRESH = 0.01
    epochs_since_improvement = 0
    val_to_improve_on = vals[0]
    for i in range(1, len(vals)):
        if vals[i] < (val_to_improve_on - SUBSTANTIAL_IMPROVEMENT_THRESH):
            val_to_improve_on = vals[i]
            epochs_since_improvement = 0
        else:
            epochs_since_improvement += 1

    return epochs_since_improvement

def get_epochs_since_val_loss_improvement(loss_record):
    
    vals = loss_record["val"]
    min_ind = np.argmin(np.array(vals))
    return (len(vals) - 1) - min_ind



def cur_epoch_is_best(loss_record, job):
    if job["training_regime"] == "fixed_num_epochs":
        key = "train"
    else:
        key = "val"
    loss_vals = loss_record[key]
    return np.argmin(loss_vals) == (len(loss_vals) - 1)



def fine_tune(job):
    
    logger = logging.getLogger(__name__)

    start_time = time.time()

    username = job["username"]
    farm_name = job["farm_name"]
    field_name = job["field_name"]
    mission_date = job["mission_date"]

    image_set_dir = os.path.join("usr", "data", username, "image_sets", farm_name, field_name, mission_date)

    tf.keras.backend.clear_session()
    
    model_dir = os.path.join(image_set_dir, "model")
    weights_dir = os.path.join(model_dir, "weights")
    training_dir = os.path.join(model_dir, "training")

    metadata_path = os.path.join(image_set_dir, "metadata", "metadata.json")
    metadata = json_io.load_json(metadata_path)


    training_record_dir = os.path.join(training_dir, "training_tf_records")
    training_tf_record_paths = glob.glob(os.path.join(training_record_dir, "*.tfrec"))

    config = create_default_config()
    config["arch"]["class_map"] = {x: i for i, x in enumerate(metadata["object_classes"])}
    model_keys.add_general_keys(config)
    model_keys.add_specialized_keys(config)

    config["training"]["active"] = {}
    for k in config["training"]:
        config["training"]["active"][k] = config["training"][k]


    train_data_loader = data_load.TrainDataLoader(training_tf_record_paths, config, shuffle=True, augment=True)
    train_dataset, num_train_patches = train_data_loader.create_batched_dataset()
    

    if job["training_regime"] == "train_val_split":
        validation_record_dir = os.path.join(training_dir, "validation_tf_records")
        val_tf_record_paths = glob.glob(os.path.join(validation_record_dir, "*.tfrec"))
        val_data_loader = data_load.TrainDataLoader(val_tf_record_paths, config, shuffle=False, augment=False)
        val_dataset, num_val_patches = val_data_loader.create_batched_dataset()

        val_loss_metric = tf.metrics.Mean()




    logger.info("Building model...")


    if config["arch"]["model_type"] == "yolov4":
        yolov4 = YOLOv4(config)
    elif config["arch"]["model_type"] == "yolov4_tiny":
        yolov4 = YOLOv4Tiny(config)

    loss_fn = YOLOv4Loss(config)


    input_shape = (config["training"]["active"]["batch_size"], *(train_data_loader.get_model_input_shape()))
    yolov4.build(input_shape=input_shape)

    logger.info("Model built.")


    cur_weights_path = os.path.join(weights_dir, "cur_weights.h5")
    best_weights_path = os.path.join(weights_dir, "best_weights.h5")


    if not os.path.exists(cur_weights_path):
        raise RuntimeError("Model weights could not be located.")


    logger.info("Loading weights...")
    yolov4.load_weights(cur_weights_path, by_name=False)
    logger.info("Weights loaded.")

    optimizer = tf.optimizers.Adam()

    train_loss_metric = tf.metrics.Mean()

    @tf.function
    def train_step(batch_images, batch_labels):
        with tf.GradientTape() as tape:
            conv = yolov4(batch_images, training=True)
            loss_value = loss_fn(batch_labels, conv)
            loss_value += sum(yolov4.losses)

        gradients = tape.gradient(target=loss_value, sources=yolov4.trainable_variables)
        optimizer.apply_gradients(grads_and_vars=zip(gradients, yolov4.trainable_variables))
        train_loss_metric.update_state(values=loss_value)


    loss_record = {
        "train": [1e8],
    }
    if job["training_regime"] == "train_val_split":
        loss_record["val"] = [1e8]


    if job["training_regime"] == "fixed_num_epochs":
        logger.info("{} ('{}'): Starting to fine-tune with {} training patches.".format(
            config["arch"]["model_type"], config["model_name"], num_train_patches
        ))
    else:
        logger.info("{} ('{}'): Starting to fine-tune with {} training patches and {} validation patches.".format(
            config["arch"]["model_type"], config["model_name"], num_train_patches, num_val_patches
        ))


    while True:

        if job["training_regime"] == "fixed_num_epochs":

            num_epochs_trained = len(loss_record["train"]) - 1
            logger.info("{} / {} epochs completed.".format(num_epochs_trained, job["num_epochs"]))

            emit.set_image_set_status(username, farm_name, field_name, mission_date,
                                      {"state_name": emit.FINE_TUNING,
                                       "progress": str(num_epochs_trained) + " / "  + str(job["num_epochs"]) + " Epochs Completed"})

            if num_epochs_trained >= job["num_epochs"]:
                end_time = time.time()
                elapsed = str(datetime.timedelta(seconds=round(end_time - start_time)))
                logger.info("Finished fine-tuning. Time elapsed: {}".format(elapsed))
                shutil.copyfile(best_weights_path, cur_weights_path)
                return
            
        else:

            epochs_since_improvement = get_epochs_since_val_loss_improvement(loss_record)

            logger.info("Epochs since validation loss improvement: {}".format(epochs_since_improvement))

            emit.set_image_set_status(username, farm_name, field_name, mission_date, 
                                        {"state_name": emit.FINE_TUNING, 
                                         "progress": "Epochs Since Improvement: " + str(epochs_since_improvement)})
            
            num_epochs_trained = len(loss_record["train"]) - 1
            if epochs_since_improvement >= job["improvement_tolerance"] or num_epochs_trained == MAX_EPOCHS_LIMIT:
                end_time = time.time()
                elapsed = str(datetime.timedelta(seconds=round(end_time - start_time)))
                
                logger.info("Finished fine-tuning. Trained for {} epochs in total. Time elapsed: {}".format(
                    num_epochs_trained, elapsed)
                )
                shutil.copyfile(best_weights_path, cur_weights_path)
                return


        for batch_data in train_dataset:

            optimizer.lr.assign(config["training"]["active"]["learning_rate_schedule"]["learning_rate"])

            batch_images, batch_labels = train_data_loader.read_batch_data(batch_data)

            train_step(batch_images, batch_labels)
            if np.isnan(train_loss_metric.result()):
                raise RuntimeError("NaN loss has occurred.")


        cur_train_loss = float(train_loss_metric.result())
        loss_record["train"].append(cur_train_loss)
        train_loss_metric.reset_states()



        if job["training_regime"] == "train_val_split":

            for batch_data in val_dataset:

                batch_images, batch_labels = val_data_loader.read_batch_data(batch_data)
                conv = yolov4(batch_images, training=False)
                loss_value = loss_fn(batch_labels, conv)
                loss_value += sum(yolov4.losses)

                val_loss_metric.update_state(values=loss_value)

                if np.isnan(val_loss_metric.result()):
                    raise RuntimeError("NaN loss has occurred.")
                

            cur_val_loss = float(val_loss_metric.result())
            loss_record["val"].append(cur_val_loss)
            val_loss_metric.reset_states()



        yolov4.save_weights(filepath=cur_weights_path, save_format="h5")
        if cur_epoch_is_best(loss_record, job):
            yolov4.save_weights(filepath=best_weights_path, save_format="h5")

        



def model_info(model_type):
    logger = logging.getLogger(__name__)

    config = create_default_config()
    model_keys.add_general_keys(config)
    model_keys.add_specialized_keys(config)

    config["training"]["active"] = {}
    for k in config["training"]:
        config["training"]["active"][k] = config["training"][k]

    logger.info("Building model...")


    if model_type == "yolov4":
        config["arch"]["model_type"] = "yolov4"
        config["arch"]["backbone_config"]["backbone_type"] = "csp_darknet53"
        config["arch"]["neck_config"]["neck_type"] = "spp_pan"
        yolov4 = YOLOv4(config)
    elif model_type == "yolov4_tiny":
        config["arch"]["model_type"] = "yolov4_tiny"
        config["arch"]["backbone_config"]["backbone_type"] = "csp_darknet53_tiny"
        config["arch"]["neck_config"]["neck_type"] = "yolov4_tiny_deconv"
        yolov4 = YOLOv4Tiny(config)
    else:
        raise RuntimeError("Invalid model type: '{}'".format(model_type))


    input_shape = (config["training"]["active"]["batch_size"], *(config["arch"]["input_image_shape"]))
    yolov4.build(input_shape=input_shape)

    yolov4.summary()


def estimate_time_for_training(job, num_training_patches):

    config = create_default_config()
    batch_size = config["training"]["batch_size"]

    if job["training_regime"] == "fixed_num_epochs":
        num_epochs = job["num_epochs"]
    else:
        num_epochs = MAX_EPOCHS_LIMIT

    max_num_batches = m.ceil(num_training_patches / batch_size) + 1

    seconds_per_batch = 1.1
    seconds_per_batch_first_epoch = 2.8

    first_epoch_time = max_num_batches * seconds_per_batch_first_epoch
    remaining_epochs_time = max_num_batches * seconds_per_batch * (num_epochs-1)
    extra_time = 60

    estimate = first_epoch_time + remaining_epochs_time + extra_time

    return convert_time_estimate_to_slurm_fmt(estimate)


def convert_time_estimate_to_slurm_fmt(estimate):


    td = datetime.timedelta(seconds=estimate)
    days = td.days
    hours = td.seconds // 3600
    minutes = (td.seconds // 60) % 60
    seconds = td.seconds - (hours * 3600) - (minutes * 60)
    
    est_str = str(days).zfill(2) + "-" + str(hours).zfill(2) + ":" + str(minutes).zfill(2) + ":" + str(seconds).zfill(2)

    return est_str

def estimate_time_for_inference(job):


    username = job["username"]
    farm_name =job["farm_name"]
    field_name = job["field_name"]
    mission_date = job["mission_date"]

    image_set_dir = os.path.join("usr", "data", username, "image_sets", farm_name, field_name, mission_date)

    model_dir = os.path.join(image_set_dir, "model")

    status_path = os.path.join(model_dir, "status.json")
    status = json_io.load_json(status_path)


    patch_overlap_percent = 50
    patch_size = status["patch_size"]
    overlap_px = int(m.floor(patch_size * (patch_overlap_percent / 100)))
    incr = patch_size - overlap_px
    max_patches = 0
    for image_index in range(len(job["image_names"])):
        for region in job["regions"][image_index]:

            region_bbox = poly_utils.get_poly_bbox(region)

            region_height = region_bbox[2] - region_bbox[0]
            region_width = region_bbox[3] - region_bbox[1]

            w_covered = max(region_width - patch_size, 0)
            num_w_patches = m.ceil(w_covered / incr) + 1

            h_covered = max(region_height - patch_size, 0)
            num_h_patches = m.ceil(h_covered / incr) + 1

            max_patches_for_region = num_w_patches * num_h_patches


            max_patches += max_patches_for_region

    
    config = create_default_config()
    batch_size = config["inference"]["batch_size"]
    num_batches = m.ceil(max_patches / batch_size)

    extra_time = 60
    prediction_time_per_batch = 1.4
    prediction_time = prediction_time_per_batch * num_batches

    estimate = extra_time + prediction_time

    return convert_time_estimate_to_slurm_fmt(estimate)



    
