# -*- coding: utf-8 -*-

import matplotlib
matplotlib.use('TkAgg')

import core.test_engine as infer_engine
from core.config import cfg
from core.config import merge_cfg_from_file
from core.config import assert_and_infer_cfg
import cv2
import utils.c2 as c2_utils
import numpy as np
import rospy
from sensor_msgs.msg import Image
from cv_bridge import CvBridge, CvBridgeError
from mobilityaids_detector.msg import Detection, Detections
from sensor_msgs.msg import CameraInfo
from visualization_msgs.msg import Marker, MarkerArray
from geometry_msgs.msg import Point, PointStamped
import tf
from std_msgs.msg import Header
from tracker import Tracker

class Detector:
    def __init__(self):
        
        train_dir = '/home/vasquez/tools/detectron_depth/final_models/RGB/VGG16-CNN-N-1024_RGB_hospital_hosponly/train/hospital_train_RGB_DepthfromDJ_new/generalized_rcnn/'
        val_dir = '/home/vasquez/tools/detectron_depth/final_models/RGB/VGG16-CNN-N-1024_RGB_hospital_hosponly/test/hospital_test2_comb_RGB_Depth/generalized_rcnn/'
        
        weights_file = train_dir + "model_final.pkl"
        config_file = "/home/vasquez/tools/detectron_depth/final_models/RGB/VGG16-CNN-N-1024_RGB_hospital_hosponly/faster_rcnn_VGG16_CNN_M_1024_RGB.yaml"
        merge_cfg_from_file(config_file)
        cfg.TEST.WEIGHTS = weights_file
        cfg.NUM_GPUS = 1
    
        assert_and_infer_cfg()
        self.model = infer_engine.initialize_model_from_cfg()
        self.bridge = CvBridge()
    
        rospy.Subscriber("/kinect2/qhd/image_color_rect", Image, self.image_callback, queue_size=1) 
        rospy.Subscriber("/kinect2/qhd/camera_info", CameraInfo, self.cam_info_callback, queue_size=1)
        
        self.image_viz_pub = rospy.Publisher("mobility_aids/image", Image, queue_size=1)
        self.rviz_viz_pub = rospy.Publisher("mobility_aids/vis", MarkerArray, queue_size=1)
        self.det_pub = rospy.Publisher("mobility_aids/detections", Detections, queue_size=1)
        self.cam_info_pub = rospy.Publisher("mobility_aids/camera_info", CameraInfo, queue_size=1)
        
        self.last_image = None
        self.new_image = False
        
        self.camera_info = None
        self.classnames = ["background", "pedestrian", "crutches", "walking_frame", "wheelchair", "push_wheelchair"]
        
        #initialize position, velocity and class tracker
        hmm_observation_model = np.loadtxt(val_dir + "observation_model.txt", delimiter=',')
        self.tracker = Tracker(hmm_observation_model)
        
        self.cla_thresholds = [0.0, 0.128528, 0.974177, 0.924291, 0.842623, 0.915776]
        
        self.tfl = tf.TransformListener()
        self.dt = -1

    def convert_from_cls_format(self, cls_boxes, cls_depths):
        """Convert from the class boxes/segms/keyps format generated by the testing
        code.
        """
        box_list = [b for b in cls_boxes if len(b) > 0]
        if len(box_list) > 0:
            boxes = np.concatenate(box_list)
        else:
            boxes = None
        if cls_depths is not None:
            depth_list = [b for b in cls_depths if len(b) > 0]
            if len(depth_list) > 0:
                depths = np.concatenate(depth_list)
            else:
                depths = None
        else:
            depths = None
        classes = []
        for j in range(len(cls_boxes)):
            classes += [j] * len(cls_boxes[j])
        return boxes, depths, classes
    
    def get_trafo_cam_in_odom(self, time):
        
        trafo_cam_in_odom = None
        
        try:
            self.tfl.waitForTransform("odom", "kinect2_rgb_optical_frame", rospy.Time(0), rospy.Duration(0.5))
            pos, quat = self.tfl.lookupTransform("odom", "kinect2_rgb_optical_frame", rospy.Time(0))
            
            trans = tf.transformations.translation_matrix(pos)
            rot = tf.transformations.quaternion_matrix(quat)
            
            #this is the transformation we get from the files in tracking
            trafo_cam_in_odom = np.dot(trans, rot)
        
        except (Exception) as e:
            print e
        
        return trafo_cam_in_odom
    
    def get_detection(self, pos, vel, confidence, category, track_id):
                
        det = Detection()
        det.category = self.classnames[category]
        det.track_id = track_id
        det.position.x = pos.x
        det.position.y = pos.y
        det.position.z = pos.z
        det.velocity.x = vel.x
        det.velocity.y = vel.y
        det.velocity.z = 0.0
        det.confidence = confidence
        
        return det
        
    def get_measurement(self, bbox, confidence, depth, category):
        
        measurement = {}
        
        im_x = (bbox[0]+bbox[2])/2
        im_y = (bbox[1]+bbox[3])/2
        
        measurement["im_x"] = im_x
        measurement["im_y"] = im_y
        measurement["depth"] = depth
        measurement["class"] = category
        
        return measurement
    
    def get_marker(self, header, position, color, marker_id, cov=None):
        
        marker = Marker()
        marker.header = header
        marker.id = marker_id
        marker.ns = "mobility_aids"
        marker.type = Marker.SPHERE
        marker.action = Marker.ADD
        marker.pose.position = position
        marker.color.b = float(color[0])/255
        marker.color.g = float(color[1])/255
        marker.color.r = float(color[2])/255
        marker.color.a = 1.0
        marker.lifetime = rospy.Duration()
        
        #no covariance info, just plot constant radius
        if cov is None:
            marker.scale.x = 0.5
            marker.scale.y = 0.5
            marker.scale.z = 0.5
            marker.pose.orientation.w = 1
            
        #plot error ellipse at sigma confidence interval
        else:
            try:
                cov = cov[0:2,0:2]
                eigenvals, eigenvecs = np.linalg.eig(cov)
                #get largest eigenvalue and eigenvector
                max_ind = np.argmax(eigenvals)
                max_eigvec = eigenvecs[:,max_ind]
                max_eigval = eigenvals[max_ind]
    
                if max_eigval < 0.00001 or max_eigval == np.nan:
                    print "max eigenval", max_eigval
                    return
                
                #get smallest eigenvalue and eigenvector
                min_ind = 0
                if max_ind == 0:
                    min_ind = 1
            
                min_eigval = eigenvals[min_ind]
            
                #chi-square value for sigma confidence interval
                chisquare_scale = 2.2789  
            
                #calculate width and height of confidence ellipse
                width = 2 * np.sqrt(chisquare_scale*max_eigval)
                height = 2 * np.sqrt(chisquare_scale*min_eigval)
                angle = np.arctan2(max_eigvec[1],max_eigvec[0])
                angle = angle + np.pi/2 #TODO is this correct?
                
                quat = tf.transformations.quaternion_from_euler(0, 0, angle)
                
                marker.pose.orientation.x = quat[0]
                marker.pose.orientation.y = quat[1]
                marker.pose.orientation.z = quat[2]
                marker.pose.orientation.w = quat[3]
                
                marker.scale.x = height
                marker.scale.y = width
                marker.scale.z = 0.1
                
            except np.linalg.linalg.LinAlgError as e:
                print 'cov', cov
                print e
        
        return marker
    
    def delete_last_markers(self):
        
        delete_marker = Marker()
        delete_markers = MarkerArray()
        delete_marker.action = Marker.DELETEALL
        delete_markers.markers.append(delete_marker)
        
        self.rviz_viz_pub.publish(delete_markers)
    
    def get_cam_calib(self):
        
        cam_calib = {}
        if self.camera_info is not None:
            #camera calibration
            cam_calib["fx"] = self.camera_info.K[0]
            cam_calib["cx"] = self.camera_info.K[2]
            cam_calib["fy"] = self.camera_info.K[4]
            cam_calib["cy"] = self.camera_info.K[5]
        
        return cam_calib
    
    def process_detections(self, image, cls_boxes, cls_depths, thresh = 0.9):
        
        measurements = []
        tracker_detections = Detections()
        tracker_detections.header = self.last_image.header
        tracker_detections.header.frame_id = "odom"
        
        boxes, depths, classes = self.convert_from_cls_format(cls_boxes, cls_depths)
        trafo_cam_in_odom = self.get_trafo_cam_in_odom(self.last_image.header.stamp)
        
        colors_box = [[1, 1, 1],
                      [39,167,0],
                      [0,0,191],
                      [0,255,255],
                      [255,0,228],
                      [101,0,255]]
        
        for i in range(len(classes)):
            bbox = boxes[i, :4]
            score = boxes[i, -1]
            cla = classes[i]
            depth = depths[i]
            
            if score > thresh[cla]:
                # draw bbox
                color_box = colors_box[cla]
                cv2.rectangle(image, (bbox[0], bbox[1]), (bbox[2], bbox[3]), color_box, 3)
                
                # fill detections message
                meas = self.get_measurement(bbox, score, depth, cla)
                measurements.append(meas)
                
        #update the tracker
        self.tracker.predict(self.dt)
        
        cam_calib = self.get_cam_calib()
        if trafo_cam_in_odom is not None:
            trafo_odom_in_cam = np.linalg.inv(trafo_cam_in_odom)
            self.tracker.update(measurements, trafo_odom_in_cam, cam_calib)

        #visualize tracks
        self.delete_last_markers()
        markers = MarkerArray()

        for track in self.tracker.tracks:
            tracker_mu = track.mu
            tracker_sigma = track.sigma

            header = self.last_image.header
            header.frame_id = "odom"

            pos = Point()
            pos.x = tracker_mu[0,0] 
            pos.y = tracker_mu[1,0] 
            pos.z = tracker_mu[2,0]

            vel = Point()
            vel.x = tracker_mu[3,0]
            vel.y = tracker_mu[4,0]
            vel.z = 0.0
            
            tracker_det = self.get_detection(pos, vel, track.hmm.get_max_score(), track.hmm.get_max_class(), track.track_id)
            tracker_detections.detections.append(tracker_det)
            
            cov = tracker_sigma[0:2,0:2]
            color_box = colors_box[track.hmm.get_max_class()]
            
            marker = self.get_marker(header, pos, color_box, track.track_id, cov)
            markers.markers.append(marker)
        
        image = self.bridge.cv2_to_imgmsg(image, encoding="passthrough")
        image.header = self.last_image.header
        
        #publish messages
        self.image_viz_pub.publish(image)
        self.rviz_viz_pub.publish(markers)
        self.cam_info_pub.publish(self.camera_info)
        self.det_pub.publish(tracker_detections)
    
    def process_last_image(self):
        
        if self.new_image:
            image = self.bridge.imgmsg_to_cv2(self.last_image, "passthrough")
            with c2_utils.NamedCudaScope(0):
                cls_boxes, cls_depths, cls_segms, cls_keyps = infer_engine.im_detect_all(
                    self.model, image, None)
            
            self.process_detections(image, cls_boxes, cls_depths, thresh=self.cla_thresholds)
            self.new_image = False

    def cam_info_callback(self, data):
        
        if self.camera_info is None:
            print "camera info received"
            self.camera_info = data

    def image_callback(self, image):
        
        if self.camera_info is not None:
            try:
                if self.last_image is not None:
                    self.dt = (image.header.stamp - self.last_image.header.stamp).to_sec()
                self.last_image = image
                self.new_image = True
                
            except CvBridgeError as e:
                print(e)
                return

def main(args):
    
    rospy.init_node('detector', anonymous=True)
    det = Detector();
    
    print "waiting for images ..."
    rate = rospy.Rate(30)
    
    while not rospy.is_shutdown():
        det.process_last_image()
        rate.sleep()
    
    print "done"
    
if __name__ == '__main__':
    main(None)
