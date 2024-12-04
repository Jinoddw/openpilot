﻿import copy
from cereal import car
from openpilot.common.params import Params #kans
from opendbc.can.can_define import CANDefine
from opendbc.can.parser import CANParser
from opendbc.car import Bus, create_button_events, structs
from opendbc.car.common.conversions import Conversions as CV
from opendbc.car.common.numpy_fast import mean
from opendbc.car.interfaces import CarStateBase
from opendbc.car.gm.values import CAR, DBC, AccState, CanBus, STEER_THRESHOLD, GMFlags, CC_ONLY_CAR, CAMERA_ACC_CAR, SDGM_CAR, CruiseButtons

ButtonType = structs.CarState.ButtonEvent.Type
TransmissionType = structs.CarParams.TransmissionType
NetworkLocation = structs.CarParams.NetworkLocation
GearShifter = structs.CarState.GearShifter
STANDSTILL_THRESHOLD = 10 * 0.0311 * CV.KPH_TO_MS
LongCtrlState = car.CarControl.Actuators.LongControlState # kans
BUTTONS_DICT = {CruiseButtons.RES_ACCEL: ButtonType.accelCruise, CruiseButtons.DECEL_SET: ButtonType.decelCruise,
                CruiseButtons.MAIN: ButtonType.mainCruise, CruiseButtons.CANCEL: ButtonType.cancel,
                CruiseButtons.GAP_DIST: ButtonType.gapAdjustCruise}

class CarState(CarStateBase):
  def __init__(self, CP):
    super().__init__(CP)
    can_define = CANDefine(DBC[CP.carFingerprint][Bus.pt])
    self.shifter_values = can_define.dv["ECMPRDNL2"]["PRNDL2"]
    self.cluster_speed_hyst_gap = CV.KPH_TO_MS / 2.
    self.cluster_min_speed = CV.KPH_TO_MS / 2.

    self.loopback_lka_steering_cmd_updated = False
    self.loopback_lka_steering_cmd_ts_nanos = 0
    self.pt_lka_steering_cmd_counter = 0
    self.cam_lka_steering_cmd_counter = 0
    self.is_metric = False

    # GAP_DIST
    self.prev_distance_button = False
    self.distance_button_pressed = False

    self.cruise_buttons = 0
    self.buttons_counter = 0
    self.single_pedal_mode = False
    self.pedal_steady = 0.

    # for delay Accfault event # 적용될 필요가 없어진 코드입니다. 아래 관련된 코드포함.
    # self.accFaultedCount = 0

    # cruiseMain default(test from nd0706-vision)
    self.cruiseMain_on = True if Params().get_int("AutoEngage") == 2 else False

  def update(self, can_parsers) -> structs.CarState:
    pt_cp = can_parsers[Bus.pt]
    cam_cp = can_parsers[Bus.cam]
    loopback_cp = can_parsers[Bus.loopback]

    ret = structs.CarState()

    prev_cruise_buttons = self.cruise_buttons
    self.prev_distance_button = self.distance_button_pressed
    if self.CP.carFingerprint not in SDGM_CAR:
      self.cruise_buttons = pt_cp.vl["ASCMSteeringButton"]["ACCButtons"]
      self.distance_button_pressed = pt_cp.vl["ASCMSteeringButton"]["DistanceButton"] != 0
      self.buttons_counter = pt_cp.vl["ASCMSteeringButton"]["RollingCounter"]
    else:
      self.cruise_buttons = cam_cp.vl["ASCMSteeringButton"]["ACCButtons"]
      self.prev_distance_button = self.distance_button_pressed
      self.distance_button_pressed = cam_cp.vl["ASCMSteeringButton"]["DistanceButton"] != 0
      self.buttons_counter = cam_cp.vl["ASCMSteeringButton"]["RollingCounter"]
    self.pscm_status = copy.copy(pt_cp.vl["PSCMStatus"])
    # GAP_DIST
    if self.cruise_buttons in [CruiseButtons.UNPRESS, CruiseButtons.INIT] and self.distance_button_pressed:
      self.cruise_buttons = CruiseButtons.GAP_DIST

    if self.CP.enableBsm:
      # kans:  not in SDGM 조건으로 해야 코드가 단순해집니다.
      if self.CP.carFingerprint not in SDGM_CAR:
        ret.leftBlindspot = pt_cp.vl["BCMBlindSpotMonitor"]["LeftBSM"] == 1
        ret.rightBlindspot = pt_cp.vl["BCMBlindSpotMonitor"]["RightBSM"] == 1
      else:
        ret.leftBlindspot = cam_cp.vl["BCMBlindSpotMonitor"]["LeftBSM"] == 1
        ret.rightBlindspot = cam_cp.vl["BCMBlindSpotMonitor"]["RightBSM"] == 1

    # Variables used for avoiding LKAS faults
    self.loopback_lka_steering_cmd_updated = len(loopback_cp.vl_all["ASCMLKASteeringCmd"]["RollingCounter"]) > 0
    if self.loopback_lka_steering_cmd_updated:
      self.loopback_lka_steering_cmd_ts_nanos = loopback_cp.ts_nanos["ASCMLKASteeringCmd"]["RollingCounter"]
    if self.CP.networkLocation == NetworkLocation.fwdCamera and not self.CP.flags & GMFlags.NO_CAMERA.value:
      self.pt_lka_steering_cmd_counter = pt_cp.vl["ASCMLKASteeringCmd"]["RollingCounter"]
      self.cam_lka_steering_cmd_counter = cam_cp.vl["ASCMLKASteeringCmd"]["RollingCounter"]

    # This is to avoid a fault where you engage while still moving backwards after shifting to D.
    # An Equinox has been seen with an unsupported status (3), so only check if either wheel is in reverse (2)
    left_whl_sign = -1 if pt_cp.vl["EBCMWheelSpdRear"]["RLWheelDir"] == 2 else 1
    right_whl_sign = -1 if pt_cp.vl["EBCMWheelSpdRear"]["RRWheelDir"] == 2 else 1
    ret.wheelSpeeds = self.get_wheel_speeds(
      left_whl_sign * pt_cp.vl["EBCMWheelSpdFront"]["FLWheelSpd"],
      right_whl_sign * pt_cp.vl["EBCMWheelSpdFront"]["FRWheelSpd"],
      left_whl_sign * pt_cp.vl["EBCMWheelSpdRear"]["RLWheelSpd"],
      right_whl_sign * pt_cp.vl["EBCMWheelSpdRear"]["RRWheelSpd"],
    )
    ret.vEgoRaw = (ret.wheelSpeeds.fl + ret.wheelSpeeds.fr + ret.wheelSpeeds.rl + ret.wheelSpeeds.rr) / 4.
    ret.vEgo, ret.aEgo = self.update_speed_kf(ret.vEgoRaw)
    # sample rear wheel speeds, standstill=True if ECM allows engagement with brake
    ret.standstill = abs(ret.wheelSpeeds.rl) <= STANDSTILL_THRESHOLD and abs(ret.wheelSpeeds.rr) <= STANDSTILL_THRESHOLD

    if pt_cp.vl["ECMPRDNL2"]["ManualMode"] == 1:
      ret.gearShifter = self.parse_gear_shifter("T")
    else:
      ret.gearShifter = self.parse_gear_shifter(self.shifter_values.get(pt_cp.vl["ECMPRDNL2"]["PRNDL2"], None))

    if self.CP.flags & GMFlags.NO_ACCELERATOR_POS_MSG.value:
      ret.brake = pt_cp.vl["EBCMBrakePedalPosition"]["BrakePedalPosition"] / 0xd0
    else:
      ret.brake = pt_cp.vl["ECMAcceleratorPos"]["BrakePedalPos"]
    if self.CP.networkLocation == NetworkLocation.fwdCamera:
      ret.brakePressed = pt_cp.vl["ECMEngineStatus"]["BrakePressed"] != 0
    else:
      # Some Volt 2016-17 have loose brake pedal push rod retainers which causes the ECM to believe
      # that the brake is being intermittently pressed without user interaction.
      # To avoid a cruise fault we need to use a conservative brake position threshold
      # https://static.nhtsa.gov/odi/tsbs/2017/MC-10137629-9999.pdf
      ret.brakePressed = ret.brake >= 8

    # Regen braking is braking
    if self.CP.transmissionType == TransmissionType.direct:
      ret.regenBraking = pt_cp.vl["EBCMRegenPaddle"]["RegenPaddle"] != 0
      self.single_pedal_mode = ret.gearShifter == GearShifter.low or pt_cp.vl["EVDriveMode"]["SinglePedalModeActive"] == 1

    if self.CP.enableGasInterceptorDEPRECATED:
      ret.gas = (pt_cp.vl["GAS_SENSOR"]["INTERCEPTOR_GAS"] + pt_cp.vl["GAS_SENSOR"]["INTERCEPTOR_GAS2"]) / 2.
      threshold = 20 if self.CP.carFingerprint in CAMERA_ACC_CAR else 4
      ret.gasPressed = ret.gas > threshold
    else:
      ret.gas = pt_cp.vl["AcceleratorPedal2"]["AcceleratorPedal2"] / 254.
      ret.gasPressed = ret.gas > 1e-5

    ret.steeringAngleDeg = pt_cp.vl["PSCMSteeringAngle"]["SteeringWheelAngle"]
    ret.steeringRateDeg = pt_cp.vl["PSCMSteeringAngle"]["SteeringWheelRate"]
    ret.steeringTorque = pt_cp.vl["PSCMStatus"]["LKADriverAppldTrq"]
    ret.steeringTorqueEps = pt_cp.vl["PSCMStatus"]["LKATorqueDelivered"]
    ret.steeringPressed = abs(ret.steeringTorque) > STEER_THRESHOLD

    # 0 inactive, 1 active, 2 temporarily limited, 3 failed
    self.lkas_status = pt_cp.vl["PSCMStatus"]["LKATorqueDeliveredStatus"]
    ret.steerFaultTemporary = self.lkas_status == 2
    ret.steerFaultPermanent = self.lkas_status == 3

    if self.CP.carFingerprint not in SDGM_CAR:
      # 1 - open, 0 - closed
      ret.doorOpen = (pt_cp.vl["BCMDoorBeltStatus"]["FrontLeftDoor"] == 1 or
                      pt_cp.vl["BCMDoorBeltStatus"]["FrontRightDoor"] == 1 or
                      pt_cp.vl["BCMDoorBeltStatus"]["RearLeftDoor"] == 1 or
                      pt_cp.vl["BCMDoorBeltStatus"]["RearRightDoor"] == 1)

      # 1 - latched
      ret.seatbeltUnlatched = pt_cp.vl["BCMDoorBeltStatus"]["LeftSeatBelt"] == 0
      ret.leftBlinker = pt_cp.vl["BCMTurnSignals"]["TurnSignals"] == 1
      ret.rightBlinker = pt_cp.vl["BCMTurnSignals"]["TurnSignals"] == 2

      ret.parkingBrake = pt_cp.vl["BCMGeneralPlatformStatus"]["ParkBrakeSwActive"] == 1
    else:
      # 1 - open, 0 - closed
      ret.doorOpen = (cam_cp.vl["BCMDoorBeltStatus"]["FrontLeftDoor"] == 1 or
                      cam_cp.vl["BCMDoorBeltStatus"]["FrontRightDoor"] == 1 or
                      cam_cp.vl["BCMDoorBeltStatus"]["RearLeftDoor"] == 1 or
                      cam_cp.vl["BCMDoorBeltStatus"]["RearRightDoor"] == 1)

      # 1 - latched
      ret.seatbeltUnlatched = cam_cp.vl["BCMDoorBeltStatus"]["LeftSeatBelt"] == 0
      ret.leftBlinker = cam_cp.vl["BCMTurnSignals"]["TurnSignals"] == 1
      ret.rightBlinker = cam_cp.vl["BCMTurnSignals"]["TurnSignals"] == 2

      ret.parkingBrake = cam_cp.vl["BCMGeneralPlatformStatus"]["ParkBrakeSwActive"] == 1

    # self.pcm_acc_status = pt_cp.vl["AcceleratorPedal2"]["CruiseState"] 아래쪽(204라인)으로 위치시키는게 accFault관련코드가 단순해집니다. 이 라인도 삭제하면 됩니다.
    # accFault지연코드는 적용될 필요가 없으므로, 삭제합니다.
    ret.cruiseState.available = pt_cp.vl["ECMEngineStatus"]["CruiseMainOn"] != 0
    self.cruiseMain_on =  ret.cruiseState.available
    ret.espDisabled = pt_cp.vl["ESPStatus"]["TractionControlOn"] != 1
    ret.accFaulted = (pt_cp.vl["AcceleratorPedal2"]["CruiseState"] == AccState.FAULTED or
                      pt_cp.vl["EBCMFrictionBrakeStatus"]["FrictionBrakeUnavailable"] == 1)

    ret.cruiseState.enabled = pt_cp.vl["AcceleratorPedal2"]["CruiseState"] != AccState.OFF
    ret.cruiseState.standstill = pt_cp.vl["AcceleratorPedal2"]["CruiseState"] == AccState.STANDSTILL
    # kans(Button or Auto Resume)
    startingState = LongCtrlState.starting
    if startingState:
      ret.cruiseState.standstill = False
    if self.CP.networkLocation == NetworkLocation.fwdCamera and not self.CP.flags & GMFlags.NO_CAMERA.value:
      if self.CP.carFingerprint not in CC_ONLY_CAR:
        ret.cruiseState.speed = cam_cp.vl["ASCMActiveCruiseControlStatus"]["ACCSpeedSetpoint"] * CV.KPH_TO_MS
      if self.CP.carFingerprint not in SDGM_CAR:
        ret.stockAeb = cam_cp.vl["AEBCmd"]["AEBCmdActive"] != 0
      else:
        ret.stockAeb = False
      # openpilot controls nonAdaptive when not pcmCruise
      if self.CP.pcmCruise:
        ret.cruiseState.nonAdaptive = cam_cp.vl["ASCMActiveCruiseControlStatus"]["ACCCruiseState"] not in (2, 3)
    if self.CP.carFingerprint in CC_ONLY_CAR:
      ret.accFaulted = False
      ret.cruiseState.speed = pt_cp.vl["ECMCruiseControl"]["CruiseSetSpeed"] * CV.KPH_TO_MS
      ret.cruiseState.enabled = pt_cp.vl["ECMCruiseControl"]["CruiseActive"] != 0
    # 위에서 삭제된 것을 아랫줄에 위치했습니다.
    self.pcm_acc_status = pt_cp.vl["AcceleratorPedal2"]["CruiseState"]
    #if True: 이 부분에서 전체적으로 true 해버리면 SPEED_RELATED_MSG.value 조건이 아무 의미가 없습니다. 일관 .96이 되어버립니다.
    #  ret.vCluRatio = 0.96
    # 아래코드는 차량속도와 계기판 속도를 일치시키기위한 Volt용. 그외 차량은 vCluRatio = 0.96으로 지정함.
    if self.CP.flags & GMFlags.SPEED_RELATED_MSG.value:
      # kans: use cluster speed & vCluRatio(longitudialPlanner)
      self.is_metric = Params().get_bool("IsMetric")
      speed_conv = CV.KPH_TO_MS * 1.609344 if self.is_metric else CV.MPH_TO_MS
      cluSpeed = pt_cp.vl["SPEED_RELATED"]["ClusterSpeed"]
      ret.vEgoCluster = cluSpeed * speed_conv
      vEgoClu, aEgoClu = self.update_clu_speed_kf(ret.vEgoCluster)
      if self.CP.carFingerprint in CAR.CHEVROLET_VOLT:
        ret.vCluRatio = (ret.vEgo / vEgoClu) if (vEgoClu > 3. and ret.vEgo > 3.) else 1.0
        #print("vCluRatio={}".format(ret.vCluRatio))
      else:
        ret.vCluRatio = 0.96

    # Don't add event if transitioning from INIT, unless it's to an actual button
    buttonEvents = [] # kans
    if self.cruise_buttons != CruiseButtons.UNPRESS or prev_cruise_buttons != CruiseButtons.INIT:
      buttonEvents.extend(create_button_events(self.cruise_buttons, prev_cruise_buttons, BUTTONS_DICT,
                                               unpressed_btn=CruiseButtons.UNPRESS)) # kans
    # kans : long_button GAP for cruise Mode(safety, ecco, high-speed..)
    if self.distance_button_pressed:
      buttonEvents.append(car.CarState.ButtonEvent(pressed=True, type=ButtonType.gapAdjustCruise))
    ret.buttonEvents = buttonEvents # kans


    return ret

  @staticmethod
  def get_can_parsers(CP):
    pt_messages = [
      ("PSCMStatus", 10),
      ("ESPStatus", 10),
      ("EBCMWheelSpdFront", 20),
      ("EBCMWheelSpdRear", 20),
      ("EBCMFrictionBrakeStatus", 20),
      ("PSCMSteeringAngle", 100),
      ("ECMAcceleratorPos", 80),
    ]
    if CP.flags & GMFlags.SPEED_RELATED_MSG.value:
      pt_messages.append(("SPEED_RELATED", 20))

    if CP.enableBsm:
      pt_messages.append(("BCMBlindSpotMonitor", 10))

    if CP.carFingerprint in SDGM_CAR:
      pt_messages += [
        ("ECMPRDNL2", 40),
        ("AcceleratorPedal2", 40),
        ("ECMEngineStatus", 80),
      ]
    else:
      pt_messages += [
        ("ECMPRDNL2", 10),
        ("AcceleratorPedal2", 33),
        ("ECMEngineStatus", 100),
        ("BCMTurnSignals", 1),
        ("BCMDoorBeltStatus", 10),
        ("BCMGeneralPlatformStatus", 10),
        ("ASCMSteeringButton", 33),
      ]

    # Used to read back last counter sent to PT by camera
    if CP.networkLocation == NetworkLocation.fwdCamera:
      pt_messages += [
        ("ASCMLKASteeringCmd", 0),
      ]
    # 이 부분은 위 조건아래 들어와야 하는 것이 아니므로 내어쓰기 해야 합니다.
    if CP.flags & GMFlags.NO_ACCELERATOR_POS_MSG.value:
      pt_messages.remove(("ECMAcceleratorPos", 80))
      pt_messages.append(("EBCMBrakePedalPosition", 100))

    if CP.transmissionType == TransmissionType.direct:
      pt_messages += [
        ("EBCMRegenPaddle", 50),
        ("EVDriveMode", 0),
      ]

    if CP.carFingerprint in CC_ONLY_CAR:
      pt_messages += [
        ("ECMCruiseControl", 10),
      ]
    if CP.enableGasInterceptorDEPRECATED: #위에 적용된 SDGM차량과 연관되어 있으므로 SDGM조건이 추가되어야 합니다.
      if CP.carFingerprint in SDGM_CAR:
        pt_messages.remove(("AcceleratorPedal2", 40)),
        pt_messages.append(("GAS_SENSOR", 50)),
      else:
        pt_messages.remove(("AcceleratorPedal2", 33)),
        pt_messages.append(("GAS_SENSOR", 50)),

    cam_messages = []
    if CP.networkLocation == NetworkLocation.fwdCamera and not CP.flags & GMFlags.NO_CAMERA.value:
      cam_messages += [
        ("ASCMLKASteeringCmd", 10),
      ]
      if CP.carFingerprint in SDGM_CAR:
        cam_messages += [
          ("BCMTurnSignals", 1),
          ("BCMDoorBeltStatus", 10),
          ("BCMGeneralPlatformStatus", 10),
          ("ASCMSteeringButton", 33),
        ]
        if CP.enableBsm:
          cam_messages.append(("BCMBlindSpotMonitor", 10))
      else:
        cam_messages += [
          ("AEBCmd", 10),
        ]
      if CP.carFingerprint not in CC_ONLY_CAR:
        cam_messages += [
          ("ASCMActiveCruiseControlStatus", 25),
        ]

    loopback_messages = [
      ("ASCMLKASteeringCmd", 0),
    ]

    return {
      Bus.pt: CANParser(DBC[CP.carFingerprint][Bus.pt], pt_messages, 0),
      Bus.cam: CANParser(DBC[CP.carFingerprint][Bus.pt], cam_messages, 2),
      Bus.loopback: CANParser(DBC[CP.carFingerprint][Bus.pt], loopback_messages, 128),
    }
