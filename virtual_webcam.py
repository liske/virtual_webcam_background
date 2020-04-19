#!/usr/bin/env python3

import tensorflow as tf
import cv2
import sys
import tfjs_graph_converter as tfjs
import numpy as np
import os
import stat
import glob
import yaml
import time
from pyfakewebcam import FakeWebcam

from scipy import ndimage

from bodypix_functions import calc_padding
from bodypix_functions import scale_and_crop_to_input_tensor_shape
from bodypix_functions import to_input_resolution_height_and_width
from bodypix_functions import to_mask_tensor
import filters

# Default config values
config = {
    "width": None,
    "height": None,
    "erode": 0,
    "blur": 0,
    "segmentation_threshold": 0.75,
    "blur_background": 0,
    "background_image": "background.jpg",
    "virtual_video_device": "/dev/video2",
    "real_video_device": "/dev/video0",
    "average_masks": 3
}

def load_config(oldconfig):
    """
        Load the config file. This only reads the file,
        when its mtime is changed.
    """

    config = oldconfig
    try:
        if os.stat("config.yaml").st_mtime != config.get("mtime"):
            config["mtime"] = os.stat("config.yaml").st_mtime
            with open("config.yaml", "r") as configfile:
                yconfig = yaml.load(configfile, Loader=yaml.SafeLoader)
                for key in yconfig:
                    config[key] = yconfig[key]
            # Force image reload
            for key in config:
                if key.endswith("_mtime"):
                    config[key] = 0
    except OSError:
        pass
    return config

def load_images(images, image_name, height, width, imageset_name,
        interpolation_method="NEAREST", image_filters=[]):
    """
        Load and preprocess image(s)
        image_name must be either the path to an image file or
        the path to an folder containing multiple files that should be
        played as animation.
        imageset_name is an unique name that is used in the config to store
        values like the mtime
        The function only reloads the image(s) when the mtime of the file
        or folder is changed.
    """
    try:
        replacement_stat = os.stat(image_name)
        if replacement_stat.st_mtime != config.get(imageset_name + "_mtime"):
            print("Loading images {0} ...".format(image_name))
            config[imageset_name + "_idx"] = 0
            filenames = [image_name]
            if stat.S_ISDIR(replacement_stat.st_mode):
                filenames = glob.glob(filenames[0] + "/*.*")
                if not filenames:
                    return None

            images = []
            for filename in filenames:
                image_raw = cv2.imread(filename, cv2.IMREAD_UNCHANGED)
                interpolation_method = cv2.INTER_LINEAR
                if interpolation_method == "NEAREST":
                    interpolation_method = cv2.INTER_NEAREST
                image = cv2.resize(image_raw, (width, height),
                    interpolation=interpolation_method)
                # BGR to RGB
                image[:,:,0], image[:,:,2] = image[:,:,2], image[:,:,0].copy()
                images.append(image)

            config[imageset_name + "_mtime"] = os.stat(image_name).st_mtime

            for i in range(len(images)):
                for image_filter in image_filters:
                    try:
                        images[i] = image_filter(images[i])
                    except TypeError:
                        # caused by a wrong number of arguments in the config
                        pass
            print("Finished loading background")

        return images

    except OSError:
        return None

def get_imagefilters(filter_list):
    image_filters = []
    for filters_item in filter_list:
        if type(filters_item) == str:
            image_filters.append(filters.get_filter(filters_item))
        if type(filters_item) == list:
            filter_name = filters_item[0]

            params = filters_item[1:]
            args = []
            kwargs = {}
            if len(params) == 1 and type(params[0]) == list:
                # ["filtername", ["value1", "value2"]]
                args = params[0]
            elif len(params) == 1 and type(params[0]) == dict:
                # ["filtername", {param1: "value1", "param2": "value2"}]
                kwargs = params[0]
            else:
                # ["filtername", "value1", "value2"]
                args = params

            _image_filter = filters.get_filter(filter_name)
            image_filters.append(
                lambda frame: _image_filter(frame, *args, **kwargs)
            )
    return image_filters

### Global variables ###

# Background frames and the current index in the list
# when the background is a animation
replacement_bgs = None

# Overlays
overlays = None

# The last mask frames are kept to average the actual mask
# to reduce flickering
masks = []

# Load the config
config = load_config(config)

### End global variables ####


# Set allow_growth for all GPUs
gpu_devices = tf.config.experimental.list_physical_devices('GPU')
for device in gpu_devices:
    tf.config.experimental.set_memory_growth(device, True)

# tf.get_logger().setLevel("DEBUG")

# VideoCapture for the real webcam
cap = cv2.VideoCapture(config.get("real_video_device"))

# Configure the resolution of the real webcam
if config.get("width"):
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, config.get("width"))
if config.get("height"):
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, config.get("height"))
# cap.set(cv2.CAP_PROP_FPS, 30)

# Get the actual resolution (either webcam default or the configured one)
width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

# Initialize a fake video device with the same resolution as the real device
fakewebcam = FakeWebcam(config.get("virtual_video_device"), width, height)

# Choose the bodypix (mobilenet) model
# Allowed values:
# - Stride 8 or 16
# internal_resolution: 0.25, 0.5, 0.75, 1.0

output_stride = 16
internal_resolution = 0.5
multiplier = 0.5

model_path = 'bodypix_mobilenet_float_{0:03d}_model-stride{1}'.format(
    int(100 * multiplier), output_stride)

# Load the tensorflow model
print("Loading model...")
graph = tfjs.api.load_graph_model(model_path)  # downloaded from the link above
print("done.")

# Setup the tensorflow session
sess = tf.compat.v1.Session(graph=graph)
input_tensor_names = tfjs.util.get_input_tensors(graph)
output_tensor_names = tfjs.util.get_output_tensors(graph)
input_tensor = graph.get_tensor_by_name(input_tensor_names[0])

def mainloop():
    global config, masks, replacement_bgs, overlays
    config = load_config(config)
    success, frame = cap.read()
    if not success:
        print("Error getting a webcam image!")
        sys.exit(1)

    if config.get("flip_horizontal"):
        frame = cv2.flip(frame, 1)
    if config.get("flip_vertical"):
        frame = cv2.flip(frame, 0)

    image_filters = get_imagefilters(config.get("background_filters", []))

    image_name = config.get("background_image", "background.jpg")
    replacement_bgs = load_images(replacement_bgs, image_name,
        height, width, "replacement_bgs",
        config.get("background_interpolation_method"),
        image_filters)

    frame = frame[...,::-1]
    if replacement_bgs is None:
        if len(image_filters) == 0:
            fakewebcam.schedule_frame(frame)
            return

        replacement_bg = np.copy(frame)
        for image_filter in image_filters:
            try:
                replacement_bg = image_filter(replacement_bg)
            except TypeError:
                # caused by a wrong number of arguments in the config
                pass

        replacement_bgs = [replacement_bg]

    input_height, input_width = frame.shape[:2]

    target_height, target_width = to_input_resolution_height_and_width(
        internal_resolution, output_stride, input_height, input_width)

    padT, padB, padL, padR = calc_padding(frame, target_height, target_width)
    resized_frame = tf.image.resize_with_pad(frame, target_height, target_width,
            method=tf.image.ResizeMethod.BILINEAR)

    resized_height, resized_width = resized_frame.shape[:2]

    # Preprocessing for resnet
    #m = np.array([-123.15, -115.90, -103.06])
    #resized_frame = np.add(resized_frame, m)

    # Preprocessing for mobilenet
    resized_frame = np.divide(resized_frame, 127.5)
    resized_frame = np.subtract(resized_frame, 1.0)
    sample_image = resized_frame[tf.newaxis, ...]

    results = sess.run(output_tensor_names,
        feed_dict={input_tensor: sample_image})
    segments = np.squeeze(results[1], 0)

    segment_logits = results[1]
    scaled_segment_scores = scale_and_crop_to_input_tensor_shape(
        segment_logits, input_height, input_width,
        padT, padB, padL, padR, True
    )

    mask = to_mask_tensor(scaled_segment_scores,
        config["segmentation_threshold"])
    mask = tf.dtypes.cast(mask, tf.int32)
    mask = np.reshape(mask, mask.shape[:2])

    # Average over the last N masks to reduce flickering
    # (at the cost of seeing afterimages)
    masks.insert(0, mask)
    num_average_masks = max(1, config.get("average_masks", 3))
    masks = masks[:num_average_masks]
    mask = np.mean(masks, axis=0)

    mask *= 255
    if config["dilate"]:
        mask = cv2.dilate(mask, np.ones((config["dilate"], config["dilate"]), np.uint8), iterations=1)
    if config["erode"]:
        mask = cv2.erode(mask, np.ones((config["erode"], config["erode"]), np.uint8), iterations=1)
    if config["blur"]:
        mask = cv2.blur(mask, (config["blur"], config["blur"]))
    mask /= 255.
    mask_inv = 1.0 - mask

    # Filter the foreground
    image_filters = get_imagefilters(config.get("foreground_filters", []))
    for image_filter in image_filters:
        try:
            frame = image_filter(frame)
        except TypeError:
            # caused by a wrong number of arguments in the config
            pass

    replacement_bgs_idx = config.get("replacement_bgs_idx", 0)
    for c in range(3):
        frame[:,:,c] = frame[:,:,c] * mask + \
            replacement_bgs[replacement_bgs_idx][:,:,c] * mask_inv

    if time.time() - config.get("last_frame_bg", 0) > 1.0 / config.get("background_fps", 1):
        config["replacement_bgs_idx"] = (replacement_bgs_idx + 1) % len(replacement_bgs)
        config["last_frame_bg"] = time.time()

    # Filter the result
    image_filters = get_imagefilters(config.get("result_filters", []))
    for image_filter in image_filters:
        try:
            frame = image_filter(frame)
        except TypeError:
            # caused by a wrong number of arguments in the config
            pass

    overlays_idx = config.get("overlays_idx", 0)
    overlays = load_images(overlays, config.get("overlay_image", ""), height, width,
        "overlays", get_imagefilters(config.get("overlay_filters", [])))

    center_of_mass = np.array(ndimage.center_of_mass(mask))
    # Attribution: http://cliparts.co/angel-halo-pictures
    halo = cv2.imread("halo.png", cv2.IMREAD_UNCHANGED)
    halo = cv2.resize(halo, (200, 100))
    center_of_halo = np.array([halo.shape[0] / 2, halo.shape[1] / 2])
    coord_from = np.clip(center_of_mass - center_of_halo,
        np.array([0,0]), np.array(frame.shape[:2])).astype(int)
    coord_to = np.clip(center_of_mass + center_of_halo,
        np.array([0, 0]), np.array(frame.shape[:2])).astype(int)

    coord_from[0] = max(coord_from[0] - 500, 0)
    coord_to[0] = coord_from[0] + halo.shape[0]

    overlay = halo
    overlay[:,:,0], overlay[:,:,2] = overlay[:,:,2], overlay[:,:,0].copy()
    for c in range(3):
        # TODO: overlay must be clipped as well
        print("test")
        frame[coord_from[0]:coord_to[0],coord_from[1]:coord_to[1],c] = frame[coord_from[0]:coord_to[0],coord_from[1]:coord_to[1],c] * (1.0 - overlay[:,:,3] / 255.0) + \
            overlay[:,:,c] * (overlay[:,:,3] / 255.0)

    if overlays:
        overlay = overlays[overlays_idx]
        assert(overlay.shape[2] == 4) # The image has an alpha channel
        for c in range(3):
            frame[:,:,c] = frame[:,:,c] * (1.0 - overlay[:,:,3] / 255.0) + \
                overlay[:,:,c] * (overlay[:,:,3] / 255.0)

        if time.time() - config.get("last_frame_overlay", 0) > 1.0 / config.get("overlay_fps", 1):
            config["overlays_idx"] = (overlays_idx + 1) % len(overlays)
            config["last_frame_overlay"] = time.time()

    if config.get("debug_show_mask", False):
        frame[:,:,0] = mask * 255
        frame[:,:,1] = mask * 255
        frame[:,:,2] = mask * 255

    fakewebcam.schedule_frame(frame)
    last_frame_time = time.time()

while True:
    try:
        mainloop()
    except KeyboardInterrupt:
        print("stopping.")
        break
