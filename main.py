#!/usr/bin/env python3

from pathlib import Path
import cv2
import depthai as dai
import numpy as np
import argparse
import time
import sys
import contextlib
import os
import random
import string

'''
Deeplabv3 obstacle detection running on selected camera.
Run as:
python3 main.py -cam rgb

Possible input choices (-cam):
'rgb', 'left', 'right'

If you have trouble with the camera connection 
you may be using a bad cable and will have
to force USB 2 mode, try "--usb 2"

Point to the desired neural network blob with
"--nn ./models/xxx.blob"

'''

num_of_classes = 2 # define the number of classes in the dataset
cam_options = ['rgb', 'left', 'right']

parser = argparse.ArgumentParser()
parser.add_argument("-cam", "--cam_input", help="select camera input source for inference", default='rgb', choices=cam_options)
parser.add_argument("-nn", "--nn_model", help="select model path for inference", default='models/v17_tfrec3_local_256_decoder_256.blob', type=str)
parser.add_argument("-usb", "--usb_mode", help="select usb 2 or 3 mode", default='3', type=int)

args = parser.parse_args()

cam_source = args.cam_input 
nn_path = args.nn_model 
usb_speed = args.usb_mode

nn_shape = 256


def decode_deeplabv3p(output_tensor):
    output = output_tensor.reshape(nn_shape,nn_shape)

    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(3,3))

    # scale to [0 ... 2555] and apply colormap
    output = np.array(output) * (255/num_of_classes)

    output = cv2.dilate(output,kernel,iterations = 1)
    output = output.astype(np.uint8)

    output_colors = cv2.applyColorMap(output, cv2.COLORMAP_JET)

    # reset the color of 0 class
    output_colors[output == 0] = [0,0,0]

    return output_colors

def show_deeplabv3p(output_colors, frame):
    return cv2.addWeighted(frame,1, output_colors,0.3,0)



# Start defining a pipeline
pipeline = dai.Pipeline()

pipeline.setOpenVINOVersion(version = dai.OpenVINO.VERSION_2021_4)

# Define a neural network that will make predictions based on the source frames
detection_nn = pipeline.create(dai.node.NeuralNetwork)
detection_nn.setBlobPath(nn_path)

detection_nn.setNumPoolFrames(5)
detection_nn.input.setBlocking(False)
detection_nn.setNumInferenceThreads(2)

cam=None


# Define a source - color camera
if cam_source == 'rgb':
    cam = pipeline.create(dai.node.ColorCamera)
    cam.setPreviewSize(nn_shape,nn_shape)
    cam.setInterleaved(False)
    cam.preview.link(detection_nn.input)
    cam.setPreviewKeepAspectRatio(True)
    #cam.initialControl.setManualFocus(200)


elif cam_source == 'left':
    cam = pipeline.create(dai.node.MonoCamera)
    cam.setBoardSocket(dai.CameraBoardSocket.LEFT)
elif cam_source == 'right':
    cam = pipeline.create(dai.node.MonoCamera)
    cam.setBoardSocket(dai.CameraBoardSocket.RIGHT)

if cam_source != 'rgb':
    manip = pipeline.create(dai.node.ImageManip)
    manip.setResize(nn_shape,nn_shape)
    manip.setKeepAspectRatio(True)
    manip.setFrameType(dai.RawImgFrame.Type.BGR888p)
    cam.out.link(manip.inputImage)
    manip.out.link(detection_nn.input)

cam.setFps(15)

# Create outputs
xout_rgb = pipeline.create(dai.node.XLinkOut)
xout_rgb.setStreamName("nn_input")
xout_rgb.input.setBlocking(False)

### set focus and advanced camera settings

detection_nn.passthrough.link(xout_rgb.input)

xout_nn = pipeline.create(dai.node.XLinkOut)
xout_nn.setStreamName("nn")
xout_nn.input.setBlocking(False)

detection_nn.out.link(xout_nn.input)

i=0

with contextlib.ExitStack() as stack:
    device_infos = dai.Device.getAllAvailableDevices()


    #### FORCE USB2 mode / necessary on Rpi W2 for example, or with bad cables

    for device_info in device_infos:
        openvino_version = dai.OpenVINO.Version.VERSION_2021_4
        if usb_speed ==3:
            usb2_mode = False
        elif usb_speed ==2:
            usb2_mode = True
        device = stack.enter_context(dai.Device(openvino_version, device_info, usb2_mode))

        # # Pipeline defined, now the device is assigned and pipeline is started
        # with dai.Device() as device:

        cams = device.getConnectedCameras()
        depth_enabled = dai.CameraBoardSocket.LEFT in cams and dai.CameraBoardSocket.RIGHT in cams
        if cam_source != "rgb" and not depth_enabled:
            raise RuntimeError("Unable to run the experiment on {} camera! Available cameras: {}".format(cam_source, cams))

        device.startPipeline(pipeline)

        # Output queues will be used to get the rgb frames and nn data from the outputs defined above
        q_nn_input = device.getOutputQueue(name="nn_input", maxSize=4, blocking=False)
        q_nn = device.getOutputQueue(name="nn", maxSize=4, blocking=False)

        
        # setting up pooling if required
        
        in_nn = q_nn.get()        
        frames_to_keep = int(1)   # This is used to pool detection frames and apply temporal smoothing if set >1
        old_frame_list = []

        for _ in range(frames_to_keep):
            frame_x = np.zeros_like(decode_deeplabv3p(np.array(in_nn.getFirstLayerInt32()).reshape(nn_shape,nn_shape)))
            old_frame_list.append(frame_x)

        while True:
            in_nn_input = q_nn_input.get()
            in_nn = q_nn.get()

            frame = in_nn_input.getCvFrame()

#            layers = in_nn.getAllLayers()

            # get layer1 data
            lay1 = np.array(in_nn.getFirstLayerInt32()).reshape(nn_shape,nn_shape)
              

            #print("this is max of lay1 "+ str(np.max(lay1)))

            output_colors = decode_deeplabv3p(lay1)

            summed_color_frames = np.add(output_colors, np.sum(frame for frame in old_frame_list))   # summing last detection with X previous detections

            frame = show_deeplabv3p(summed_color_frames, frame) # weighted sum frame for display
            
            frame = cv2.resize(frame, (300, 300))

            isolated_masks = cv2.resize((np.sum(frame for frame in old_frame_list)), (300, 300))

            isolated_masks_BW = cv2.cvtColor(isolated_masks,cv2.COLOR_BGR2GRAY)

            thresh = cv2.threshold(src=isolated_masks_BW,
                       thresh=127,
                       maxval=255,
                       type=cv2.THRESH_BINARY_INV)[1]

            thresh = cv2.bitwise_not(thresh)

            #cv2.imshow("mask_only",thresh)
            
            
            try:
                blank_mask = np.zeros(thresh.shape, dtype=np.uint8)
                cnts = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                cnts = cnts[0] if len(cnts) == 2 else cnts[1]
                cnts = sorted(cnts, key=cv2.contourArea, reverse=True)[0]
                
                #print(int(cv2.contourArea(cnts)))

                if int(cv2.contourArea(cnts)) >= 25000:                
                    cv2.fillPoly(blank_mask, [cnts], 255)
                    out = blank_mask            
                    # calculate moments of binary image
                    M = cv2.moments(out)
                    # calculate x,y coordinate of center
                    cX = int(M["m10"] / M["m00"])
                    cY = int(M["m01"] / M["m00"])
                    cv2.circle(out, (cX, cY), 30, (0, 0, 0), -1)
                    cv2.circle(out, (cX, cY), 28, (255, 255, 255), -1)  
                    cv2.putText(out, "drop!", (cX -20, cY),cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 0, 0), 2)
                else:
                    cv2.fillPoly(blank_mask, [cnts], 255)
                    out = blank_mask        
                    cv2.putText(out, "drop sites too small", (5, frame.shape[0] - 10),cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 2)
            except:
                out = np.zeros(thresh.shape, np.uint8)
                cv2.putText(out, "no drop site candidates!", (5, frame.shape[0] - 10),cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 2)


            cv2.imshow("SELECTED_LANDING_SPOT",out)


            ### ALT BW MASK

            # stacked_binary_masks,thresh1 = cv2.threshold(cv2.cvtColor((np.sum(frame for frame in old_frame_list)), cv2.COLOR_RGB2GRAY),5,255,cv2.THRESH_BINARY)
            
            # cv2.imshow("mask_only", stacked_binary_masks)
 
            old_frame_list.append(output_colors)
            old_frame_list.pop(0)


            if cv2.waitKey(1) == ord('q'):
                break
            i+=1
