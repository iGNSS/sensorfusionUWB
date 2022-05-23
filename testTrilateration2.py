import gtsam
from DataSets.extractData import RosDataTrilateration
from DataSets.extractGt import GroundTruthEstimates
from gtsam.symbol_shorthand import X, L, V, B
import numpy as np
from settings import DATASET_NUMBER
from DataTypes.uwb_position import UWB_Ancors_Descriptor

from scipy.spatial.transform import Rotation as R
from Sensors.IMU import IMU
from Sensors.GNSS import GNSS

import matplotlib.pyplot as plt
from Utils.gtsam_pose_utils import gtsam_pose_from_result, gtsam_landmark_from_results, gtsam_bias_from_results, gtsam_velocity_from_results
from Plotting.plot_gtsam import plot_horizontal_trajectory, plot_position, plot_angels, plot_bias, plot_vel, plot_threedof2, plot_threedof_error, new_xy_plot, ATE
import seaborn as sns

from uwbPreinitializationTuning import *


class GtSAMTest:

    def __init__(self) -> None:
        self.dataset: RosDataTrilateration = RosDataTrilateration(DATASET_NUMBER)
        isam_params: gtsam.ISAM2Params = gtsam.ISAM2Params()
        isam_params.setFactorization("QR")
        isam_params.setRelinearizeSkip(1)
        self.isam: gtsam.ISAM2 = gtsam.ISAM2(isam_params)
        self.uwb_positions: UWB_Ancors_Descriptor = UWB_Ancors_Descriptor(
            DATASET_NUMBER)
        self.ground_truth: GroundTruthEstimates = GroundTruthEstimates(
            DATASET_NUMBER, pre_initialization=False)
        self.imu_params: IMU = IMU()
        self.gnss_params: GNSS = GNSS()

        # Tracked variables for IMU and UWB
        self.pose_variables: list = []
        self.velocity_variables: list = []
        self.imu_bias_variables: list = []
        self.landmarks_variables: dict = {}
        self.uwb_counter: int = 0
        self.time_stamps: list = []

        # Setting up gtsam values
        self.graph_values: gtsam.Values = gtsam.Values()
        self.factor_graph: gtsam.NonlinearFactorGraph = gtsam.NonlinearFactorGraph()
        self.initialize_graph()
        sns.set()

    def initialize_graph(self):

        # Defining the state
        X1 = X(0)
        V1 = V(0)
        B1 = B(0)
        self.pose_variables.append(X1)
        self.velocity_variables.append(V1)
        self.imu_bias_variables.append(B1)

        # Set priors
        self.prior_noise_x = gtsam.noiseModel.Diagonal.Sigmas(
            PRIOR_POSE_SIGMAS)
        self.prior_noise_v = gtsam.noiseModel.Diagonal.Sigmas(PRIOR_VEL_SIGMAS)
        self.prior_noise_b = gtsam.noiseModel.Diagonal.Sigmas(
            PRIOR_BIAS_SIGMAS)
        R_init = R.from_euler("xyz", self.ground_truth.initial_pose()[
                              :3], degrees=False).as_matrix()
        T_init = self.ground_truth.initial_pose()[3:]
        T_init[2] = DOWN_INITIAL_VALUE

        self.current_pose = gtsam.Pose3(gtsam.Rot3(R_init), T_init)

        self.current_velocity = self.ground_truth.initial_velocity()
        self.current_bias = gtsam.imuBias.ConstantBias(
            np.zeros((3,)), BIAS_INITIAL_VALUE)
        self.navstate = gtsam.NavState(self.current_pose.rotation(), self.current_pose.translation(
        ), self.current_pose.rotation().matrix().T @ self.current_velocity)

        self.factor_graph.add(gtsam.PriorFactorPose3(
            X1, self.current_pose, self.prior_noise_x))
        self.factor_graph.add(gtsam.PriorFactorVector(
            V1, self.current_velocity, self.prior_noise_v))
        self.factor_graph.add(gtsam.PriorFactorConstantBias(
            B1, self.current_bias, self.prior_noise_b))

        self.graph_values.insert(X1, self.current_pose)
        self.graph_values.insert(V1, self.current_velocity)
        self.graph_values.insert(B1, self.current_bias)
        self.time_stamps.append(self.ground_truth.time[0])

    def add_UWB_to_graph(self, graph, uwb_measurement):

        pose = gtsam.Pose3(self.current_pose.rotation(), uwb_measurement.position)
        graph.add(gtsam.PriorFactorPose3(self.pose_variables[-1], pose, uwb_measurement.noise_model))

        return pose

    def reset_pose_graph_variables(self):
        self.graph_values = gtsam.Values()
        self.factor_graph = gtsam.NonlinearFactorGraph()
        self.uwb_counter = 0

    def pre_integrate_imu_measurement(self, imu_measurements):
        summarized_measurement = gtsam.PreintegratedImuMeasurements(
            self.imu_params.preintegration_param, self.current_bias)

        deltaT = 1 / self.dataset.dataset_settings.imu_frequency

        for measurement in imu_measurements:
            summarized_measurement.integrateMeasurement(
                measurement.linear_vel, measurement.angular_vel, deltaT)

        return summarized_measurement

    def add_imu_factor(self, integrated_measurement, imu_measurements):
        # Create new state variables
        self.pose_variables.append(X(len(self.pose_variables)))
        self.velocity_variables.append(V(len(self.velocity_variables)))
        self.imu_bias_variables.append(B(len(self.imu_bias_variables)))

        # Add the new factors to the graph
        self.factor_graph.add(gtsam.ImuFactor(
            self.pose_variables[-2],
            self.velocity_variables[-2],
            self.pose_variables[-1],
            self.velocity_variables[-1],
            self.imu_bias_variables[-2],
            integrated_measurement
        ))

        self.factor_graph.add(
            gtsam.BetweenFactorConstantBias(
                self.imu_bias_variables[-2],
                self.imu_bias_variables[-1],
                self.current_bias,
                gtsam.noiseModel.Diagonal.Sigmas(
                    np.sqrt(len(imu_measurements)) * self.imu_params.sigmaBetweenBias)
            )
        )

        self.navstate = integrated_measurement.predict(self.navstate, self.current_bias)
        velocityNED = self.navstate.pose().rotation().matrix() @ self.navstate.velocity()
        velocityNED[2] = 0

        self.graph_values.insert(self.pose_variables[-1], self.navstate.pose())
        self.graph_values.insert(self.velocity_variables[-1], velocityNED)
        self.graph_values.insert(self.imu_bias_variables[-1], self.current_bias)

        self.factor_graph.add(gtsam.PriorFactorVector(
            self.velocity_variables[-1], velocityNED, gtsam.noiseModel.Diagonal.Sigmas(np.sqrt(len(imu_measurements))*VELOCITY_SIGMAS)))
        self.factor_graph.add(gtsam.PriorFactorConstantBias(
            self.imu_bias_variables[-1], self.current_bias, self.prior_noise_b))
        self.factor_graph.add(gtsam.PriorFactorPose3(self.pose_variables[-1], self.navstate.pose(
        ), gtsam.noiseModel.Diagonal.Sigmas(np.sqrt(len(imu_measurements))*POSE_SIGMAS)))

    def run(self):
        # Dummy variable for storing imu measurements
        imu_measurements = []
        iteration_number = 0
        length_of_preinitialization = len(self.pose_variables)
        for measurement in self.dataset.generate_trilateration_combo_measurements():

            if measurement.measurement_type.value == "UWB_Tri":
                if imu_measurements:
                    self.time_stamps.append(measurement.time)
                    integrated_measurement = self.pre_integrate_imu_measurement(
                        imu_measurements)
                    self.add_imu_factor(
                        integrated_measurement, imu_measurements)

                    # Reset the IMU measurement list
                    imu_measurements = []
                self.uwb_counter += 1
                uwb_pose = self.add_UWB_to_graph(self.factor_graph, measurement)
                #self.graph_values.insert(self.pose_variables[-1], uwb_pose)
                #self.graph_values.insert(self.velocity_variables[-1], self.current_velocity)
                #self.graph_values.insert(self.imu_bias_variables[-1], self.current_bias)

                # if not (700 < len(self.pose_variables) < 1100):
                #    self.add_UWB_to_graph(measurement)

            elif measurement.measurement_type.value == "IMU":
                # Store the IMU factors unntil a new UWB measurement is recieved
                imu_measurements.append(measurement)

            iteration_number += 1
            print("Iteration", iteration_number, len(
                self.pose_variables), len(self.time_stamps))

            # Update ISAM with graph and initial_values
            if self.uwb_counter == 1:

                self.isam.update(self.factor_graph, self.graph_values)
                result = self.isam.calculateEstimate()
                positions, eulers = gtsam_pose_from_result(result)

                # Reset the graph and initial values
                self.reset_pose_graph_variables()

                self.current_pose = result.atPose3(self.pose_variables[-1])
                self.current_velocity = result.atVector(
                    self.velocity_variables[-1])
                self.current_velocity[2] = 0
                self.current_bias = result.atConstantBias(
                    self.imu_bias_variables[-1])
                self.navstate = gtsam.NavState(self.current_pose.rotation(), self.current_pose.translation(
                ), self.current_pose.rotation().matrix().T @ self.current_velocity)
                if len(self.pose_variables) > 760:
                    break

        self.isam.update(self.factor_graph, self.graph_values)
        result = self.isam.calculateBestEstimate()
        positions, eulers = gtsam_pose_from_result(result)

        # Compensate for UWB arm
        uwb_offset = np.array([3.285, -2.10, -1.35]).reshape((3, 1))
        gnss_offset = np.array([3.015, 0, -1.36])

        for index in range(len(positions[length_of_preinitialization:])):
            positions[length_of_preinitialization + index] -= (R.from_euler("xyz", eulers[length_of_preinitialization + index]).as_matrix() @ uwb_offset).flatten()

        biases = gtsam_bias_from_results(result, self.imu_bias_variables)
        print("ATE: ", ATE(positions, self.ground_truth, self.time_stamps))

        print("\n-- Plot pose")
        plt.figure(1)
        plot_threedof2(positions, eulers, self.ground_truth, self.time_stamps)
        plt.figure(2)
        plot_threedof_error(positions, eulers, self.ground_truth, self.time_stamps)
        plt.figure(3)
        new_xy_plot(positions, eulers, self.ground_truth, self.time_stamps)
        plt.show()

        print("ATE: ", ATE(positions, self.ground_truth, self.time_stamps))


testing = GtSAMTest()
testing.run()