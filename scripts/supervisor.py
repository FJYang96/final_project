#!/usr/bin/env python

import rospy
from gazebo_msgs.msg import ModelStates
from std_msgs.msg import Float32MultiArray, String
from geometry_msgs.msg import Twist, PoseArray, Pose2D, PoseStamped
from sensor_msgs.msg import Image, CameraInfo, LaserScan
from asl_turtlebot.msg import DetectedObject, ObjectLocation
import tf
import math
from enum import Enum

# if sim is True/using gazebo, therefore want to subscribe to /gazebo/model_states\
# otherwise, they will use a TF lookup (hw2+)
use_gazebo = False

# if using gmapping, you will have a map frame. otherwise it will be odom frame
mapping = rospy.get_param("map")


# threshold at which we consider the robot at a location
POS_EPS = .15
THETA_EPS = 1
DIS_THRES = .3

# time to stop at a stop sign
STOP_TIME = 3

# minimum distance from a stop sign to obey it
STOP_MIN_DIST = .5

# time taken to cross an intersection
CROSSING_TIME = 3

# minimum time for discovery
DISCOVER_TIME = 60

# state machine modes, not all implemented
class Mode(Enum):
    IDLE = 1
    POSE = 2
    STOP = 3
    CROSS = 4
    NAV = 5
    MANUAL = 6

    PICK = 7    # pick food up
    DELI = 8    # deliver food


print("supervisor settings:\n")
print("use_gazebo = %s\n" % use_gazebo)
print("mapping = %s\n" % mapping)

class Supervisor:

    def __init__(self):
        rospy.init_node('turtlebot_supervisor_nav', anonymous=True)
        # initialize variables
        self.x = 0
        self.y = 0
        self.theta = 0
        self.mode = Mode.IDLE
        self.last_mode_printed = None
        self.trans_listener = tf.TransformListener()
        #############
        self.init_x, self.init_y, self.init_theta = 0., 0., 0.
        
        self.start_time = rospy.get_rostime()
        self.discover_finished = False
        if not use_gazebo:
            try:
                origin_frame = "/map" if mapping else "/odom"
                (translation,rotation) = self.trans_listener.lookupTransform(origin_frame, '/base_footprint', rospy.Time(0))
                self.init_x = translation[0]
                self.init_y = translation[1]
                euler = tf.transformations.euler_from_quaternion(rotation)
                self.init_theta = euler[2]
            except (tf.LookupException, tf.ConnectivityException, tf.ExtrapolationException):
                pass
        #############

        # command pose for controller
        self.pose_goal_publisher = rospy.Publisher('/cmd_pose', Pose2D, queue_size=10)
        # nav pose for controller
        self.nav_goal_publisher = rospy.Publisher('/cmd_nav_supervisor', Pose2D, queue_size=10)
        # command vel (used for idling)
        self.cmd_vel_publisher = rospy.Publisher('/cmd_vel', Twist, queue_size=10)

        # subscribers
        
        # Self edited, camera inforation
        self.focal_length = 1.
        rospy.Subscriber('/camera/camera_info', CameraInfo, self.camera_info_callback)
        
        # stop sign detector
        rospy.Subscriber('/detector/stop_sign', DetectedObject, self.stop_sign_detected_callback)
        # high-level navigation pose
        rospy.Subscriber('/nav_pose', Pose2D, self.nav_pose_callback)
        # if using gazebo, we have access to perfect state
        if use_gazebo:
            rospy.Subscriber('/gazebo/model_states', ModelStates, self.gazebo_callback)
        # we can subscribe to nav goal click
        rospy.Subscriber('/move_base_simple/goal', PoseStamped, self.rviz_goal_callback)
        # subscribe to requested food from TA
        rospy.Subscriber('/delivery_request', String, self.delivery_request_callback)
        self.requested_food = []
        self.deliver_finished = False

#################################################################################################
        rospy.Subscriber('/food_location', ObjectLocation, self.update_food_location_callback)
        self.food_name = []
        self.food_x= []
        self.food_y = []

        self.detect_status = False

    def update_food_location_callback(self, msg):
      	if not self.discover_finished:
          self.food_name = list(msg.name)
          self.food_x = list(msg.x)
          self.food_y = list(msg.y)

    def delivery_request_callback(self, msg):
        if not self.discover_finished:
            return

        self.requested_food = str(msg.data).split(',')
        self.requested_food.append('not in list')
        print('Received Request', self.requested_food)

        self.mode = Mode.DELI
        print('entering deliver loop')
        self.deliver_finished = False
        while not self.deliver_loop():
            print(self.x_g, self.y_g)
            pass
        print('deliver finished')
    
    def camera_info_callback(self, msg):
        self.focal_length = msg.K[4]

###############################################################################################
    def gazebo_callback(self, msg):
        pose = msg.pose[msg.name.index("turtlebot3_burger")]
        twist = msg.twist[msg.name.index("turtlebot3_burger")]
        self.x = pose.position.x
        self.y = pose.position.y
        quaternion = (
                    pose.orientation.x,
                    pose.orientation.y,
                    pose.orientation.z,
                    pose.orientation.w)
        euler = tf.transformations.euler_from_quaternion(quaternion)
        self.theta = euler[2]

    def rviz_goal_callback(self, msg):
        """ callback for a pose goal sent through rviz """
        if self.discover_finished:
            return
        origin_frame = "/map" if mapping else "/odom"
        print("rviz command received!")
        try:
            
            nav_pose_origin = self.trans_listener.transformPose(origin_frame, msg)
            self.x_g = nav_pose_origin.pose.position.x
            self.y_g = nav_pose_origin.pose.position.y
            quaternion = (
                    nav_pose_origin.pose.orientation.x,
                    nav_pose_origin.pose.orientation.y,
                    nav_pose_origin.pose.orientation.z,
                    nav_pose_origin.pose.orientation.w)
            euler = tf.transformations.euler_from_quaternion(quaternion)
            self.theta_g = euler[2]
        except (tf.LookupException, tf.ConnectivityException, tf.ExtrapolationException):
            pass
        self.mode = Mode.NAV

    def nav_pose_callback(self, msg):
        self.x_g = msg.x
        self.y_g = msg.y
        self.theta_g = msg.theta
        self.mode = Mode.NAV

    def stop_sign_detected_callback(self, msg):
        """ callback for when the detector has found a stop sign. Note that
        a distance of 0 can mean that the lidar did not pickup the stop sign at all """

        # distance of the stop sign
        # dist = msg.distance
        dist = self.focal_length * 0.09 / (msg.corners[2]-msg.corners[0])

        # if close enough and in nav mode, stop
        if dist > 0 and dist < STOP_MIN_DIST and self.mode == Mode.NAV:
            self.init_stop_sign()

    def go_to_pose(self):
        """ sends the current desired pose to the pose controller """

        pose_g_msg = Pose2D()
        pose_g_msg.x = self.x_g
        pose_g_msg.y = self.y_g
        pose_g_msg.theta = self.theta_g

        self.pose_goal_publisher.publish(pose_g_msg)

    def nav_to_pose(self):
        """ sends the current desired pose to the naviagtor """

        nav_g_msg = Pose2D()
        nav_g_msg.x = self.x_g
        nav_g_msg.y = self.y_g
        nav_g_msg.theta = self.theta_g
        #print('In nav to pose:', self.x_g, self.y_g)

        self.nav_goal_publisher.publish(nav_g_msg)

    def stay_idle(self):
        """ sends zero velocity to stay put """

        vel_g_msg = Twist()
        vel_g_msg.linear.x = 0.; vel_g_msg.linear.y = 0.; vel_g_msg.linear.z = 0.
        vel_g_msg.angular.x = 0.; vel_g_msg.angular.y = 0.; vel_g_msg.angular.z = 0.
        self.cmd_vel_publisher.publish(vel_g_msg)

    def close_to(self,x,y,theta):
        """ checks if the robot is at a pose within some threshold """

        return (abs(x-self.x)<POS_EPS and abs(y-self.y)<POS_EPS and abs(theta-self.theta)<THETA_EPS)

    def init_stop_sign(self):
        """ initiates a stop sign maneuver """

        self.stop_sign_start = rospy.get_rostime()
        self.mode = Mode.STOP
        
    def init_food_stop(self):
        self.stop_sign_start = rospy.get_rostime()
        self.mode = Mode.PICK

    def has_stopped(self):
        """ checks if stop sign maneuver is over """

        return (self.mode == Mode.STOP and (rospy.get_rostime()-self.stop_sign_start)>rospy.Duration.from_sec(STOP_TIME))
        
    def init_crossing(self):
        """ initiates an intersection crossing maneuver """

        self.cross_start = rospy.get_rostime()
        self.mode = Mode.CROSS

    def has_crossed(self):
        """ checks if crossing maneuver is over """

        return (self.mode == Mode.CROSS and (rospy.get_rostime()-self.cross_start)>rospy.Duration.from_sec(CROSSING_TIME))

    def is_discover_finished(self):
      	if (rospy.get_rostime() - self.start_time > rospy.Duration.from_sec(DISCOVER_TIME) and \
            ((self.x - self.init_x)**2 + (self.y - self.init_y)**2)**0.5 < DIS_THRES):
            self.discover_finished = True
            self.mode = Mode.IDLE
            rospy.loginfo("Finish discover! Ready to start")
      
    def discover_loop(self):
        """ the main loop of the robot. At each iteration, depending on its
        mode (i.e. the finite state machine's state), if takes appropriate
        actions. This function shouldn't return anything """

        if not use_gazebo:
            try:
                origin_frame = "/map" if mapping else "/odom"
                (translation,rotation) = self.trans_listener.lookupTransform(origin_frame, '/base_footprint', rospy.Time(0))
                self.x = translation[0]
                self.y = translation[1]
                euler = tf.transformations.euler_from_quaternion(rotation)
                self.theta = euler[2]
            except (tf.LookupException, tf.ConnectivityException, tf.ExtrapolationException):
                pass

        # logs the current mode
        if not(self.last_mode_printed == self.mode):
            rospy.loginfo("Current Mode: %s", self.mode)
            self.last_mode_printed = self.mode

        # checks wich mode it is in and acts accordingly
        if self.mode == Mode.IDLE:
            # send zero velocity
            self.is_discover_finished()
            self.stay_idle()

        elif self.mode == Mode.POSE:
            # moving towards a desired pose
            if self.close_to(self.x_g,self.y_g,self.theta_g):
                self.mode = Mode.IDLE
            else:
                self.go_to_pose()

        elif self.mode == Mode.STOP:
            # at a stop sign
            if self.has_stopped():
                self.init_crossing()
            else:
                self.stay_idle()

        elif self.mode == Mode.CROSS:
            # crossing an intersection
            if self.has_crossed():
                self.mode = Mode.NAV
            else:
                self.nav_to_pose()

        elif self.mode == Mode.NAV:
            if self.close_to(self.x_g,self.y_g,self.theta_g):
                self.mode = Mode.IDLE
            else:
                self.nav_to_pose()
        else:
            raise Exception('This mode is not supported: %s'
                % str(self.mode))
            
    def set_goal_pose(self):
        this_food = 'not in list'
        while(this_food not in self.food_name and len(self.requested_food) > 0):
            this_food = self.requested_food.pop(0)

        if(len(self.requested_food) > 0):
            food_ind = self.food_name.index(this_food)
            self.x_g = self.food_x[food_ind]
            self.y_g = self.food_y[food_ind]
            self.theta_g = 0.
        else:
            self.x_g = 0.0
            self.y_g = 0.0
            self.theta_g = 0.
            self.deliver_finished = True
        self.mode = Mode.NAV

        print('Heading towards', this_food, 'at', self.x_g, self.y_g)
    
    def deliver_loop(self):
        """ the main loop of the robot. At each iteration, depending on its
        mode (i.e. the finite state machine's state), if takes appropriate
        actions. This function shouldn't return anything """

        if not use_gazebo:
            try:
                origin_frame = "/map" if mapping else "/odom"
                (translation,rotation) = self.trans_listener.lookupTransform(origin_frame, '/base_footprint', rospy.Time(0))
                self.x = translation[0]
                self.y = translation[1]
                euler = tf.transformations.euler_from_quaternion(rotation)
                self.theta = euler[2]
            except (tf.LookupException, tf.ConnectivityException, tf.ExtrapolationException):
                pass

        # logs the current mode
        if not(self.last_mode_printed == self.mode):
            rospy.loginfo("Current Mode: %s", self.mode)
            self.last_mode_printed = self.mode

        # checks wich mode it is in and acts accordingly
        if self.mode == Mode.IDLE:
            # send zero velocity
            self.stay_idle()
            if self.deliver_finished:
                return True

        elif self.mode == Mode.POSE:
            # moving towards a desired pose
            if (abs(x-self.x)<0.5 and abs(y-self.y)<0.5 and abs(theta-self.theta)<1.5):
                self.mode = Mode.IDLE
                self.init_food_stop()
            else:
                self.go_to_pose()

        elif self.mode == Mode.STOP:
            # at a stop sign
            if self.has_stopped():
                self.init_crossing()
            else:
                self.stay_idle()
                
        elif self.mode == Mode.PICK:
            if self.has_stopped():
              	self.mode = Mode.DELI
            else:
                if self.deliver_finished:
                    return True
              	self.stay_idle()

        elif self.mode == Mode.CROSS:
            # crossing an intersection
            if self.has_crossed():
                self.mode = Mode.NAV
            else:
                self.nav_to_pose()

        elif self.mode == Mode.NAV:
            if self.close_to(self.x_g,self.y_g,self.theta_g):
                self.mode = Mode.IDLE
                self.init_food_stop()
            else:
                self.nav_to_pose()
                
        elif self.mode == Mode.DELI:
            self.set_goal_pose()
            self.mode = Mode.NAV

        else:
            raise Exception('This mode is not supported: %s'
                % str(self.mode))

        return False

    def run(self):
        rate = rospy.Rate(10) # 10 Hz
        while not rospy.is_shutdown():
            if self.discover_finished:
              	break
            self.discover_loop()
            rate.sleep()
            
        while not rospy.is_shutdown():
            # while rospy.get_rostime() - self.start_time < rospy.Duration.from_sec(30) and \
            #     ((self.x - self.food_x[-1])**2 + (self.y - self.food_y[-1])**2)**0.5 > DIS_THRES:
            #     print('entering deliver loop')
            #     self.deliver_loop()
                rate.sleep()

if __name__ == '__main__':
    sup = Supervisor()
    sup.run()
