# import the opencv library
import cv2
import math
import numpy as np

# Look at code blocks marked with LIMELIGHT_DEBUG to find places where an
# adjustment may be needed.

# Set this to 1 for testing the code with a screenshot of the limelight camrea
# frame captured from the limelight.local:5801 console.
DEBUG_LIMELIGHT_SCREENSHOT = 0

class CoordinateFrame(object):
  def __init__(self, axisLength):
    self.coordFrame = np.zeros((4, 3), np.float32)
    self.coordFrame[0, ...] = [0, 0, 0]
    self.coordFrame[1, ...] = [1, 0, 0]
    self.coordFrame[2, ...] = [0, -1, 0]
    self.coordFrame[3, ...] = [0, 0, -1]
    self.coordFrame *= axisLength
    

  def Draw(self, frame, rvec, tvec, calib):
    coords, _ = cv2.projectPoints(
        self.coordFrame, rvec, tvec, calib.cameraMatrix, calib.distCoeffs)
    # Draw coordinate axes.
    coords = [tuple(np.squeeze(x).astype(int)) for x in coords.tolist()]
    cv2.arrowedLine(frame, coords[0], coords[1], (  0, 255, 0), 2)
    cv2.arrowedLine(frame, coords[0], coords[2], (255, 255, 0), 2)
    cv2.arrowedLine(frame, coords[0], coords[3], (0, 255, 255), 2)


class Chessboard(object):
  def __init__(self, squareWidth, rows, cols):
    self.chessPoints = np.zeros((rows * cols, 3), np.float32)
    self.chessPoints[:,:2] = (
        np.mgrid[0:cols, 0:rows].T.reshape(-1,2) * squareWidth)
    self.patternSize = (cols, rows)
    self.squareWidth = squareWidth


  def GetObjectAndImagePoints(self, frame, out_frame=None):
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    found, corners = cv2.findChessboardCorners(
        gray, self.patternSize,
        flags = cv2.CALIB_CB_ADAPTIVE_THRESH + cv2.CALIB_CB_NORMALIZE_IMAGE + cv2.CALIB_CB_FAST_CHECK
    )
    if found:
      cv2.cornerSubPix(gray, corners, (11, 11), (-1, -1),
          (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.01))
      if out_frame is not None:
        cv2.drawChessboardCorners(out_frame, self.patternSize, corners, found)
      return self.chessPoints, corners
    else:
      return None, None

  def SquareWidth(self):
    return self.squareWidth


class ChessboardTracker(object):
  def __init__(self, calib, squareWidth, rows, cols):
    self.chessboard = Chessboard(squareWidth, rows, cols)
    self.calib = calib
    self.cam_ext = CameraExtrinsics(calib)


  def localize(self, frame, out_frame):
    obj_pts, img_pts = self.chessboard.GetObjectAndImagePoints(frame, out_frame)
    success = self.cam_ext.estimate_pose(obj_pts, img_pts)
    if success and out_frame is not None:
      self.draw(obj_pts, out_frame)
    return self.cam_ext


  def draw(self, obj_pts, out_frame):
    rvec, tvec = self.cam_ext.rvec_tvec()

    # Draw checkerboard points.
    checkerboard, _ = cv2.projectPoints(
        obj_pts, rvec, tvec, self.calib.cameraMatrix, self.calib.distCoeffs)
    for point in checkerboard:
      point = np.squeeze(point)
      point = tuple(int(p) for p in point)
      if abs(point[0]) < 2000 and abs(point[1]) < 2000:
        cv2.circle(out_frame, point, 4, (255, 0, 0), 2)

    # Draw common extrinsics information.
    self.cam_ext.draw(out_frame,
                      axis_length=self.chessboard.SquareWidth() * 2)

    
class CameraExtrinsics(object):
  def __init__(self, calib):
    self.reset()
    self.calib = calib

  
  def reset(self):
    self.rvec = None
    self.tvec = None
    self.trackingSuccess = False
    self.distance = 0
    self.yaw = 0
    return self


  def rvec_tvec(self):
    rvec = self.rvec.copy() if self.rvec is not None else None
    tvec = self.tvec.copy() if self.tvec is not None else None
    return rvec, tvec


  def update_rvec_tvec(self, rvec, tvec):
    success = self.verify_tracking_output(rvec, tvec)
    if success:
      self.rvec = rvec
      self.tvec = tvec
      self.trackingSuccess = True
      self.compute_farness_and_turnness()
    else:
      self.reset()
    return success


  def is_valid(self):
    return self.trackingSuccess


  def estimate_pose(self, obj_pts, img_pts, use_undistorted_pts=False):
    if obj_pts is None or img_pts is None:
      self.reset()
      return False

    # Make a copy of rvec and tvec before passing to SolvePnP,
    # else it is updated in-place inside SolvePnP.
    rvec, tvec = self.rvec_tvec()

    if use_undistorted_pts:
      img_pts = cv2.undistortPoints(
          img_pts, self.calib.cameraMatrix, self.calib.distCoeffs)
      img_pts = np.squeeze(img_pts)
      cameraMatrix = np.identity(3)
      distCoeffs = self.calib.distCoeffs * 0
    else:
      cameraMatrix = self.calib.cameraMatrix
      distCoeffs = self.calib.distCoeffs

    success, rvec, tvec = cv2.solvePnP(
        obj_pts, img_pts, cameraMatrix, distCoeffs,
        rvec=rvec, tvec=tvec, useExtrinsicGuess=self.trackingSuccess)

    return self.update_rvec_tvec(rvec, tvec)
     

  def verify_tracking_output(self, new_rvec, new_tvec):
    # Distance between old and new tvec length should not be too large.
    # tvec is vector from camera center to hub. Only check if previous
    # tracking was successful.
    if self.trackingSuccess:
      max_delta = 12  # inches.
      delta_dist = abs(np.linalg.norm(new_tvec) -
                       np.linalg.norm(self.tvec))
      if delta_dist > max_delta:
        print(f'Delta distance {delta_dist} too high: ')
        return False

    return True
 

  def compute_farness_and_turnness(self):
    # Translation and rotation for world -> camera.
    Twc = np.squeeze(self.tvec)
    Rwc, _ = cv2.Rodrigues(self.rvec)
    
    def cam2world(point):
      #    p_c == Rwc * p_w + Twc
      #    p_c - Twc = Rwc * p_w
      #    p_w = transpose(Rwc) * (p_c - Twc)
      return np.matmul(np.transpose(Rwc), (point - Twc))

    # Camera origin and orientation (z-vector) in world coords.
    cam_loc = cam2world(np.asarray([0, 0, 0]))
    cam_orient = cam2world(np.asarray([0, 0, 1])) - cam_loc
    #print(cam_loc)
    #print(cam_orient)

    # Project vector from camera (cam_loc) to world origin (0,0,0) on x-z plane.
    #cam2orig_x, cam2orig_z = (-cam_loc[0], -cam_loc[2])
    cam2orig_xz = np.asarray([-cam_loc[0], -cam_loc[2]])

    # Project camera z-vector on x-z plane.
    cam_orient_xz = np.asarray([cam_orient[0], cam_orient[2]])

    # https://math.stackexchange.com/questions/317874/
    # calculate-the-angle-between-two-vectors
    #
    # Let 𝑎=(𝑥1,𝑦1), 𝑏=(𝑥2,𝑦2). If 𝜃 is the "oriented" angle from 𝑎 to 𝑏
    # (that is, rotating 𝑎̂  by 𝜃 gives 𝑏̂ ), then: 𝜃=atan2(𝑥1𝑦2−𝑦1𝑥2,𝑥1𝑥2+𝑦1𝑦2)
    # This is the same as tan(t) = sin(t)/cos(t) = v1xv2/v1.v2
    #
    # Find angle to rotate camera orientation to align with cam2origin vector.
    yaw = math.atan2(np.cross(cam_orient_xz, cam2orig_xz),
                     np.dot(cam_orient_xz, cam2orig_xz))
    self.yaw = yaw * 180/np.pi

    # Compute pitch. Similar to above but first compute the angle t between
    # 3D vectors: camera orientation (in world coorinates) and world negative
    # y-axis (pointing upwards). 
    # Then, pitch = 90 - t, where
    # tan(t) = sin(t)/cos(t) = v1xv2/v1.v2, which implies
    # tan(90 - t) = cot(t) = cos(t) / sin(t) = v1.v2 / v1xv2.
    y_axis = np.asarray([0, -1, 0])
    pitch = math.atan2(np.dot(cam_orient, y_axis),
                       np.linalg.norm(np.cross(cam_orient, y_axis)))
    self.pitch = pitch * 180/np.pi

    # Horizontal distance from camera center to hub.
    #self.distance = np.linalg.norm(np.asarray([hub_x, hub_z]))
    self.distance = np.linalg.norm(cam2orig_xz)

    # Save interim data.
    self.cam_loc = cam_loc
    self.cam_orient = cam_orient

    return True #self.yaw, self.pitch, self.distance


  def draw(
      self, out_frame, axis_length=5, orig_color=(0, 0, 255), text_yoffset=0):
    rvec, tvec = self.rvec_tvec()

    # Draw center and coordinate axes.
    if rvec is not None and tvec is not None:
      origin = np.asarray([0, 0, 0], dtype=np.float32)
      proj_circle, _ = cv2.projectPoints(
          origin, rvec, tvec, self.calib.cameraMatrix, self.calib.distCoeffs) 

      proj_circle = np.squeeze(proj_circle)
      center = (int(proj_circle[0]), int(proj_circle[1]))
      if abs(center[0]) < 2000 and abs(center[1]) < 2000:
        #cv2.circle(frame, center, 6, orig_circle_color, 3)
        cv2.circle(out_frame, center, 6, (0, 0, 255), 3)

      coord_frame = CoordinateFrame(axis_length)
      coord_frame.Draw(out_frame, rvec, tvec, self.calib)

    # Draw the angle and distance.
    go = self.distance >= 90 and self.distance <= 102
    if go:
      go_color = (0, 255, 255)
    else:
      go_color = (255, 255, 255)

    spin = self.yaw >= -10 and self.yaw <= 10
    if spin:
      spin_color = (0, 255, 255)
    else:
      spin_color = (255, 255, 255)

    yaw_text = 'Camera yaw: ' + str(int(self.yaw * 10) / 10) + 'deg'
    cv2.putText(
        out_frame, yaw_text, (10, text_yoffset+30), cv2.FONT_HERSHEY_SIMPLEX, 1, #0.75,
        spin_color, 2)
    pitch_text = 'Camera pitch: ' + str(int(self.pitch * 10) / 10) + 'deg'
    cv2.putText(
        out_frame, pitch_text, (10, text_yoffset+60), cv2.FONT_HERSHEY_SIMPLEX, 1, #0.75,
        (255, 255, 255), 2)
    dist_text = 'Horizontal dist: ' + str(int(self.distance * 10) / 10) + 'in'
    cv2.putText(
        out_frame, dist_text, (10, text_yoffset+90), cv2.FONT_HERSHEY_SIMPLEX, 1, #0.75,
        go_color, 2)
    
    if go and spin:
      cv2.putText(
          out_frame, "SHOOT", (160, 120), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 5)


class Hub(object):
  def __init__(self,
               calib,
               height=None,
               cam_height=None,
               cam_pitch=None,
               radius=26.6875,
               tape_length=5,
               gap_length=5.5,
               tape_height=2,
               num_tapes=16):
    self.calib = calib
    self.height = height
    self.cam_height = cam_height
    self.cam_pitch = cam_pitch
    self.radius = radius
    self.tape_length = tape_length
    self.gap_length = gap_length
    self.tape_height = tape_height
    self.num_tapes = num_tapes
    self.tapes  = self.set_vis_tape_coords()
    self.cam_ext = CameraExtrinsics(calib)
    self.quads = None
    self.frame = None

  
  def set_vis_tape_coords(self):
    total_rads = 0
    tape_rads = self.tape_length / self.radius
    gap_rads = self.gap_length / self.radius

    tapes = []
    for i in range(self.num_tapes):
      tape = []
      tape.append((math.cos(total_rads) * self.radius,
                   -self.tape_height / 2,
                   math.sin(total_rads) * self.radius))

      tape.append((math.cos(total_rads) * self.radius,
                   self.tape_height / 2,
                   math.sin(total_rads) * self.radius))

      total_rads += tape_rads

      tape.append((math.cos(total_rads) * self.radius,
                   -self.tape_height / 2,
                   math.sin(total_rads) * self.radius))

      tape.append((math.cos(total_rads) * self.radius,
                   self.tape_height / 2,
                   math.sin(total_rads) * self.radius))

      # Change order of tape points to be:
      # 0  1
      # 3  2
      # We constructed them as:
      # 0  2
      # 1  3
      # Mapping:
      # So, permute original order to (0, 2, 3, 1)
      new_order = [0, 2, 3, 1]
      tape = [tape[i] for i in new_order]


      tapes.append(np.asarray(tape))

      total_rads += gap_rads

    return tapes

  
  def localize_known_config(self, quads, out_frame=None):
    if self.height is None or self.cam_height is None or self.cam_pitch is None:
      print('Scene config not known.')
      return self.cam_ext.reset()

    if len(quads) < 1:
      return self.cam_ext.reset()

    img_pts = [pt for pts in quads for pt in pts]
    img_pts = np.asarray(img_pts)
   
    # Represent img_pts in normalized coords:
    # x,y location corresponds to the 3D ray (x,y,1) in the camera
    # coordinate system.
    img_pts = cv2.undistortPoints(
          img_pts, self.calib.cameraMatrix, self.calib.distCoeffs)
    img_pts = np.squeeze(img_pts)
    
    # Negate y, so it increases as it goes towards the top of the image. 
    img_y = -img_pts[:, 1]
    img_x =  img_pts[:, 0]

    # Find max y => closest tape corner. Alse a top corner.
    img_y.sort()

    y_theta_rad = np.arctan(img_y[-1])
    y_theta = 180 * y_theta_rad / np.pi
    total_angle = y_theta + self.cam_pitch
    total_angle_rad = total_angle * np.pi / 180

    h_z_ratio = np.tan(total_angle_rad)

    cam_tape_height = self.height + self.tape_height / 2 - self.cam_height

    if h_z_ratio > 0:
      hdist = cam_tape_height / h_z_ratio + self.radius
    else:
      hdist = 0
    self.cam_ext.distance = hdist

    mean_x = np.mean(img_x)
    x_theta_rad = np.arctan(mean_x)
    x_theta = 180 * x_theta_rad / np.pi
    self.cam_ext.yaw = x_theta

    self.cam_ext.pitch = self.cam_pitch

    if out_frame is not None:
      self.cam_ext.draw(out_frame)

    return self.cam_ext
 

  def localize(self, quads, out_frame=None):
    """
    Args:
      quads: Track tape quadrilaterals in the image.
      dR: delta rotation between previous and current frame.
          dR transforms points from prev-camera CS to curr-camera CS
          (CS: Coordinate Systetm).
          Can be None, if not known.
      dt: delta translation between previous and current frame.
          dt is a unit vector, since only direction of translation is known.
          Can be None, if not known.
    """
    if len(quads) < 2:
      return self.cam_ext.reset()

    num_quads = min(len(quads), len(self.tapes))

    obj_quads = self.tapes[:num_quads]

    # Use with limelight screenshot. Assumes that the second quad has the
    # limelight "detected diamond", which means it is either not detected as a
    # quad, or it has the wrong shape, so we want to ignore that quad.
    # Hence skip the second quad and tape.
    if DEBUG_LIMELIGHT_SCREENSHOT:
      obj_quads = [self.tapes[0], self.tapes[2]]
      if len(quads) > 2:
        quads = [quads[0], quads[2]]

    img_pts = [pt for pts in quads for pt in pts]
    obj_pts = [pt for pts in obj_quads for pt in pts]

    img_pts = np.asarray(img_pts)
    obj_pts = np.asarray(obj_pts)

    success = self.cam_ext.estimate_pose(obj_pts, img_pts)

    if success and out_frame is not None:
      self.draw(out_frame, draw_hub=True)

    return self.cam_ext


  def draw(self, out_frame, text_yoffset=0, color_mode=0, draw_hub=True):
    if color_mode == 0:
      poly_circle_color = (0, 127, 255)
      poly_line_color = (0, 255, 255)
      orig_circle_color = (0, 0, 255)
    else:
      poly_circle_color = (127, 255, 0)
      poly_line_color = (255, 0, 127)
      orig_circle_color = (255, 0, 0)

    rvec, tvec = self.cam_ext.rvec_tvec()
    yaw = self.cam_ext.yaw
    pitch = self.cam_ext.pitch
    distance = self.cam_ext.distance

    Twc = np.squeeze(tvec)
    Rwc, _ = cv2.Rodrigues(rvec)
    def world2cam(point):
      return np.matmul(Rwc, point) + Twc

    # Draw projected hub.
    if draw_hub:
      proj_tapes = []
      hub_loc = world2cam(np.asarray([0, 0, 0]))
      hub_z = hub_loc[2]
      for i,tape in enumerate(self.tapes):
        tape = [world2cam(t) for t in tape]
        tape = np.asarray(tape, dtype=np.float32)
        tape_z = np.mean(tape, axis=0)[2]
        proj_tape, _ = cv2.projectPoints(tape,
                                         rvec * 0,
                                         tvec * 0,
                                         self.calib.cameraMatrix,
                                         self.calib.distCoeffs)
        color_scale = (hub_z - tape_z + 30) / 20
        line_color = [c * color_scale for c in poly_line_color]
        proj_tapes.append(proj_tape)
        draw_polygon(proj_tape,
                     out_frame,
                     #index_start=4*i,
                     #draw_index_scale=2,
                     line_color=line_color,
                     circle_color=poly_circle_color,
                     circle_radius=4,
                     circle_thickness=2)
    
    # Draw common camera extrinsic items.
    self.cam_ext.draw(out_frame,
                      axis_length=self.radius/3,
                      orig_color=orig_circle_color,
                      text_yoffset=text_yoffset)

 
def extract_polygon(contour):
  # Arc-length based eps seems to work better on vision tape.
  eps = 0.005 * cv2.arcLength(contour, True)
  #eps = 5 #3
  polygon = cv2.approxPolyDP(contour, eps, True)
  return polygon


def draw_polygon(polygon,
                 frame,
                 line_color,
                 circle_color=None,
                 circle_radius=5,
                 circle_thickness=2,
                 draw_index=False,
                 draw_index_scale=0,
                 index_start=0):
  def is_outside(x, y):
    # Check if point is way outside. Can happen if projected point lies
    # at infinity due to a bad result from SolvePnP.
    return (x < -frame.shape[1] or
            y < -frame.shape[0] or
            x > 2 * frame.shape[1] or
            y > 2 * frame.shape[0])

  polygon = [np.squeeze(p) for p in list(polygon)]
  npts = len(polygon)
  for i in range(npts):
    x1 = polygon[i][0]
    y1 = polygon[i][1]
    x2 = polygon[(i+1)%npts][0]
    y2 = polygon[(i+1)%npts][1]
    if is_outside(x1, y1) or is_outside(x2, y2):
      return
    x1 = int(x1)
    y1 = int(y1)
    x2 = int(x2)
    y2 = int(y2)
    cv2.line(frame, (x1, y1), (x2, y2), line_color, 2)
    if circle_color is not None:
      cv2.circle(frame, (x1, y1), circle_radius, circle_color, circle_thickness)
    if draw_index_scale > 0:
      cv2.putText(frame, str(i+index_start), (x1, y1), cv2.FONT_HERSHEY_SIMPLEX,
                  0.5 * draw_index_scale, (0, 0, 0))
  

def merge_nearby_points(polygon, min_dist):
  merged = []
  for p in polygon:
    #if len(merged) == 0 or np.linalg.norm(merged[-1] - ip[1]) >= min_dist:   
    if len(merged) == 0 or np.linalg.norm(merged[-1] - p) >= min_dist:   
      merged.append(p)
  return np.asarray(merged)


def sort_quad(quad):
  # https://www.pyimagesearch.com/2016/03/21/ordering-coordinates-clockwise-with-python-and-opencv/
  xSorted = quad[np.argsort(quad[:, 0]), :]
  leftMost = xSorted[:2, :]
  rightMost = xSorted[2:, :]
  leftMost = leftMost[np.argsort(leftMost[:, 1]), :]
  (tl, bl) = leftMost
  dists = [np.linalg.norm(tl - p) for p in rightMost]
  (br, tr) = rightMost[np.argsort(dists)[::-1], :]
  return (tl, tr, br, bl)


def reorder_quads(quads):
  # Use largest quad to filter other quads.
  #max_quad_area = 0
  #for q in quads:


  x_pos = []
  for q in quads:
    q = list(np.squeeze(q))
    x_pos.append(q[0][0])

  if len(x_pos) > 0:
    quads = np.asarray(quads)[np.argsort(x_pos), :]
  return list(quads)

def interp_by_x(p1, p2, x):
  # Find alpha a, s.t.
  # x = (1 - a) * p1.x + a * p2.x
  # x = a * (p2.x - p1.x) + p1.x
  # a = (x - p1.x) / (p2.x - p1.x)
  # y = (1 - a) * p1.y + a * p2.y
  d12 = p2[0] - p1[0]
  d1x = x - p1[0]
  if abs(d12) < 1e-6:
    return p1 if fabs(d1x) < fabs(x - p2[0]) else p2 
  a = d1x / d12
  y = (1 - a) * p1[1] + a * p2[1]
  return [x, y]


def extract_quad(polygon,
                 mask,
                 find_contour_corners=False,
                 use_cosines_for_corners=False,
                 assume_zero_roll=True,
                 make_fixed_height=False):
  if not test_area(polygon):
    return False, None

  if not test_border_proximity(polygon, mask):
    return False, None

  polygon_rect = cv2.minAreaRect(polygon)
  if not test_aspect_ratio(polygon_rect):
    return False, None

  box_points = cv2.boxPoints(polygon_rect)
  box_points = sort_quad(box_points)

  if assume_zero_roll:
    # For zero roll, the left and right edges should be perfectly vertical,
    # so force the top & bottom corners to have the same x-value on each side.
    # Since minAreaRect returns a bounding rectangle, we can take the tighter
    # x-values on each side and they should still enclose the curve (if the
    # roll is actually zero), resulting in a parallelogram with vertical sides.
    tl, tr, br, bl = box_points
    xl = max(tl[0], bl[0])
    xr = min(tr[0], br[0])
    tl = interp_by_x(tl, tr, xl)
    bl = interp_by_x(bl, br, xl)
    tr = interp_by_x(tl, tr, xr)
    br = interp_by_x(bl, br, xr)
    box_points = tl, tr, br, bl

  if make_fixed_height:
    tl, tr, br, bl = box_points
    dy_l = bl[1] - tl[1]
    dy_r = br[1] - tr[1]
    #dy = min(dy_l, dy_r)
    dy = max(dy_l, dy_r)
    tl[1] = bl[1] - dy
    tr[1] = br[1] - dy
    box_points = tl, tr, br, bl

  if not find_contour_corners:
    return box_points, True

  if use_cosines_for_corners:
    cosines = get_polygon_cosines(polygon)

  box_matches = [[],[],[],[]]
  for i,p in enumerate(polygon):
    box_dists = []
    for box_p in box_points:
      dist = np.linalg.norm(p-box_p)
      box_dists.append(dist)
    index = np.argmin(box_dists)
    p = np.squeeze(p)
    sort_key = cosines[i] if use_cosines_for_corners else bost_dists[index]
    box_matches[index].append((sort_key, p))

  quad = []
  found_quad = True
  for i in range(4):
    if len(box_matches[i]) == 0:
      found_quad = False
      break
    best = min(box_matches[i], key=lambda x: x[0])
    quad.append(best[1])

  # Corner sub-pix.
  if found_quad:
    scale = min(mask.shape[0], mask.shape[1]) / 360
    window = 3 * int(2 * scale) + 1
    quad = cv2.cornerSubPix(
        mask,
        np.asarray(quad, dtype=np.float32),
        (window, window),
        (-1, -1),
        (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 40, 0.001))

  return quad, found_quad

  
def get_polygon_cosines(polygon):
  points = list(np.squeeze(polygon))
  points2 = points[1:] + points[:1]   # Next, with rotation.
  points0 = points[-1:] + points[:-1] # Prev, with rotation.
  cosines = []
  for p0, p, p2 in zip(points0, points, points2):
    v01 = p - p0
    v12 = p2 - p
    norm = np.linalg.norm(v01) * np.linalg.norm(v12)
    cosine = 1 if norm == 0 else np.dot(v01, v12) / norm
    #cosines.append(abs(cosine)) # to change later?
    cosines.append(cosine) # to change later?
  return cosines


def test_area(polygon,
              min_area=150,
              min_hull_area_ratio=0.9,
              #min_hull_area_ratio=0.95,
              max_hull_area_ratio=1.01):
  # Area matching convex hull.
  area = cv2.contourArea(polygon)
  if area < min_area:
    return False
  hull_area = cv2.contourArea(cv2.convexHull(polygon))
  area_ratio = area / hull_area
  if area_ratio < min_hull_area_ratio:
    return False
  if area_ratio > max_hull_area_ratio:
    return False
  return True


def test_aspect_ratio(rect,
                    min_aspect_ratio=1.2,
                    #min_aspect_ratio=1.5,
                    max_aspect_ratio=10.0,
                    max_horz_angle=60):
  (x, y), (width, height), angle = rect
  cos_angle = np.cos(angle * np.pi / 180)
  cos_45 = np.cos(45 * np.pi / 180)
  if cos_angle > cos_45:
    aspect_ratio = width / height
  else:
    aspect_ratio = height / width

  if aspect_ratio < min_aspect_ratio:
    return False
  if aspect_ratio > max_aspect_ratio:
    return False

  # Since we assume camera to have zero roll, the
  # angle of the rectangle should be close to 0.
  if angle > 45:
    angle = 90 - angle
  
  if angle > max_horz_angle:
    return False

  return True


def test_border_proximity(polygon, mask, border=10):
  polygon = list(np.squeeze(polygon))
  width = mask.shape[1]
  height = mask.shape[0]
  for p in polygon:
    if (p[0] < border or p[0] > width - border or
        p[1] < border or p[1] > height - border):
      return False
  return True


def detect_green(hsv):
  #low_green = np.array([35, 100, 175])
  #high_green = np.array([80, 255, 255])
  
  # Good range
  #low_green = np.array([35, 100, 15])
  #high_green = np.array([80, 255, 255])
  
  # LIMELIGHT_DEBUG: Play with v value (15 - 100 range)
  low_green = np.array([35, 100, 15])
  #low_green = np.array([35, 100, 75])
  high_green = np.array([80, 255, 255])

  mask = cv2.inRange(hsv, low_green, high_green)
  """
  kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
  mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
  """
  return mask


def detect_white(rgb):
  thresh = 100
  mask = (rgb[:,:,0] > thresh) & (rgb[:,:,1] > thresh) & (rgb[:,:,2] > thresh)
  mask = (mask * 255).astype(np.uint8)
  return mask


def detect_tapes(frame, out_frame):
  #blurred = cv2.GaussianBlur(frame, (7, 7), 0)
  blurred = cv2.GaussianBlur(frame, (3, 3), 0)
  hsv = cv2.cvtColor(blurred, cv2.COLOR_BGR2HSV)
  mask = detect_green(hsv)
  #mask = detect_white(blurred)
  #blurred_mask = cv2.GaussianBlur(mask, (3, 3), 0)

  # Find contours
  cnts = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
  cnts = cnts[0] if len(cnts) == 2 else cnts[1]

  quads = []
  for c in cnts:
    # LIMELIGHT_DEBUG: Play with arc length.
    if cv2.arcLength(c, True) < 50: #100:
      continue
    """
    p = extract_polygon(c)
    p = merge_nearby_points(p, 5)
    p, found = extract_quad(p, blurred_mask)
    """
    p, found = extract_quad(c, mask)
    if not found:
      continue
    if out_frame is not None:
      cv2.drawContours(out_frame, [c], -1, (255, 255, 255), 1)
    quads.append(np.squeeze(p))

  quads = reorder_quads(quads)
  if out_frame is not None:
    for i,q in enumerate(quads):
      draw_polygon(q, out_frame, (0, 0, 255), draw_index_scale=1, index_start=i*4)

  return quads, mask, hsv, blurred

# Define globals: calibration and hub object.
class LimelightCalib(object):
  def __init__(self, imageHeight):
    # Matrix for 960x720
    self.cameraMatrix = np.asarray([[772.53876202, 0., 479.132337442],
                        [0., 769.052151477, 359.143001808],
                        [0., 0., 1.0]])
    self.distCoeffs = np.asarray([
        2.9684613693070039e-01, -1.4380252254747885e+00, -2.2098421479494509e-03,
        -3.3894563533907176e-03, 2.5344430354806740e+00])
    
    # Adjust for new height.
    self.cameraMatrix *= imageHeight / 720
    self.cameraMatrix[2, 2] = 1.0


# LIMELIGHT_DEBUG: Adjust based on situation.
# Set imageHeight based on resolution used. But
# if using 240, LimelightCalib will need to be adjusted.
_imageHeight_ll = 720
#_imageHeight_ll = 240 

# Limelight camera calib.
_calib_ll = LimelightCalib(_imageHeight_ll)


# LIMELIGHT_DEBUG: Adjust based on situation.
#
# Different params used for hub testing. (uncomment the correct one).
#
# Testing inside KLS with hub on whiteboards with 2nd chassis
"""
_hub_height = 70.6
_cam_height = 6.5
_cam_pitch = 9
"""
# Real hub with test chassis.
"""
_hub_height = 104
_cam_height = 6.5
_cam_pitch = 9.5  # ?? 12.9 ?
"""
# Real hub with real robot. Values derived in testing
_hub_height = 104
_cam_height = 29.7
_cam_pitch = 31 # CAD suggested 26.5 but testing indicates 31.

_hub_ll = Hub(_calib_ll, height=_hub_height, cam_height=_cam_height, cam_pitch=_cam_pitch)


# LIMELIGHT_DEBUG: Adjust based on chessboard size
# (used for calibration or pitch angle calculation).
#
# Chess data based on what we used for calibrating Nihal's camera.
_chess_squareWidth = 0.9212598  # in inches, converted from 2.34cm
_chess_rows = 6
_chess_cols = 9

_chess_ll = ChessboardTracker(_calib_ll, _chess_squareWidth, _chess_rows, _chess_cols)

# IMPORTANT NOTES:
#   This Python pipeline should be run on shooting limelight
#   Network: 6962
#   DNS URL: http://limelight-shoot.local:5801/ (Drive/Ball-tracking is limelight-drive).
#   IP: 10.69.62.11 (Drive/Ball-tracking is 10.69.62.12).
#   Web UI is on port 5801, video stream is on port 5800, and raw video stream is on 5802.
#
#   Select Pipeline 5 to run hub tracking script.
#   Exposure: 2
#   Black-leve: 11-25
#   Red/Blue balance: Defaults, but if needed, increase the red balance and decrease the
#     blue balance, so that only the tape is green.

# runPipeline() is called every frame by Limelight's backend.
frame_idx = 0
def runPipeline(frame,
                llrobot,
                hub=_hub_ll,
                chess=_chess_ll,
                target='hub',
                use_localize_known_config=True):
  """Called by limelight framework for every frame. Estimates yaw and distance.

  Args:
    frame: Input frame.
    llrobot: Passed by limelight framework (not used).
    hub: Hub instance. Defaults to hub_ll defined above.
    chess: Chess instance. Defaults to chess_ll defined above.
    target: 'hub' to track hub, or 'chess' to track a chessboard. Latter can be
        used to estimate camera pitch and any distance offsets by tracking a
        perfectly vertical chessboard.
    use_localize_known_config: Selects the localize function. If True, uses the
        known hub and camera height and camera pitch.
  """
  global frame_idx
  frame_idx = (frame_idx + 1) % 1000000
  if frame_idx % 300 == 0:
    print('frame', frame_idx)

  out_frame = frame.copy()

  if target == 'hub':
    quads, mask, hsv, blurred = detect_tapes(frame, out_frame=out_frame)
    if use_localize_known_config:
      cam_ext = hub.localize_known_config(quads, out_frame=out_frame)
    else:
      cam_ext = hub.localize(quads, out_frame=out_frame)
    largest_contour = quads
  elif target == 'chess':
    cam_ext = chess.localize(frame, out_frame=out_frame)
    largest_contour = []
  else:
    raise ValueError('Unknown target', target)

  output = cam_ext.yaw, cam_ext.distance

  if output[0] != 0 or output[1] != 0:
    print(output)

  return largest_contour, out_frame, output
