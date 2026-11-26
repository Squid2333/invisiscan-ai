import cv2
import numpy as np
from collections import deque

from .basetrack import BaseTrack, TrackState
from .kalman_filter import KalmanFilter
from . import matching

import cv2
import numpy as np

def compute_hsv_hist(img_bgr, tlbr, bins=(16, 16, 16)):
    x1, y1, x2, y2 = map(int, tlbr)
    h, w = img_bgr.shape[:2]
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(w - 1, x2), min(h - 1, y2)
    if x2 <= x1 or y2 <= y1:
        return None

    crop = img_bgr[y1:y2, x1:x2]
    if crop.size == 0:
        return None

    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1, 2], None, bins, [0, 180, 0, 256, 0, 256])
    cv2.normalize(hist, hist)
    return hist.flatten().astype(np.float32)

def cosine_sim(a, b):
    if a is None or b is None:
        return -1.0
    den = (np.linalg.norm(a) * np.linalg.norm(b) + 1e-6)
    return float(np.dot(a, b) / den)

def app_distance(tracks, dets):
    """
    返回 [len(tracks), len(dets)] 的 appearance cost 矩阵
    cost 越小越像：
      cost = 1 - clamp(cosine_sim, 0, 1)
    """
    T, D = len(tracks), len(dets)
    out = np.ones((T, D), dtype=np.float32)  # 默认最差 cost=1

    if T == 0 or D == 0:
        return out

    for i, t in enumerate(tracks):
        tf = getattr(t, "reid_feat", None)
        if tf is None:
            continue
        for j, d in enumerate(dets):
            df = getattr(d, "reid_feat", None)
            if df is None:
                continue
            sim = cosine_sim(tf, df)   # [-1,1]
            sim = max(0.0, min(1.0, sim))
            out[i, j] = 1.0 - sim      # [0,1]
    return out

def size_ratio_cost(tracks, dets):
    """
    返回 cost 矩阵（0好 1坏），用于惩罚尺寸差异过大的匹配
    """
    T, D = len(tracks), len(dets)
    C = np.zeros((T, D), dtype=np.float32)
    if T == 0 or D == 0:
        return C

    for i, t in enumerate(tracks):
        tb = t.tlbr
        tw = max(1.0, tb[2] - tb[0])
        th = max(1.0, tb[3] - tb[1])
        ta = tw * th
        for j, d in enumerate(dets):
            db = d.tlbr
            dw = max(1.0, db[2] - db[0])
            dh = max(1.0, db[3] - db[1])
            da = dw * dh

            # 比例：小框/大框（越接近1越好）
            r = min(ta, da) / max(ta, da)
            C[i, j] = 1.0 - r   # r=1 => cost=0; r很小 => cost接近1
    return C

class STrack(BaseTrack):
    shared_kalman = KalmanFilter()

    def __init__(self, tlwh, score, depth=np.nan):
        # original
        self._tlwh = np.asarray(tlwh, dtype=np.float32)
        self.kalman_filter = None
        self.mean, self.covariance = None, None
        self.is_activated = False

        self.score = score
        self.tracklet_len = 0

        # >>> DEPTH PATCH
        self.depth = float(depth)

        self.reid_feat = None  # HSV histogram
        self.last_image = None  # 可选：保存一张 crop

    def predict(self):
        mean_state = self.mean.copy()
        if self.state != TrackState.Tracked:
            mean_state[7] = 0
        self.mean, self.covariance = self.kalman_filter.predict(
            mean_state, self.covariance
        )

    @staticmethod
    def multi_predict(stracks):
        if len(stracks) > 0:
            multi_mean = np.asarray([st.mean.copy() for st in stracks])
            multi_cov = np.asarray([st.covariance for st in stracks])
            for i, st in enumerate(stracks):
                if st.state != TrackState.Tracked:
                    multi_mean[i][7] = 0
            multi_mean, multi_cov = STrack.shared_kalman.multi_predict(
                multi_mean, multi_cov
            )
            for i, st in enumerate(stracks):
                st.mean = multi_mean[i]
                st.covariance = multi_cov[i]

    @staticmethod
    def tlbr_to_tlwh(tlbr):
        """
        tlbr: [x1, y1, x2, y2]
        tlwh: [x, y, w, h]  where (x,y) is top-left
        """
        ret = np.asarray(tlbr, dtype=np.float32).copy()
        ret[2] = ret[2] - ret[0]
        ret[3] = ret[3] - ret[1]
        return ret

    @staticmethod
    def tlwh_to_tlbr(tlwh):
        """
        tlwh: [x, y, w, h]
        tlbr: [x1, y1, x2, y2]
        """
        ret = np.asarray(tlwh, dtype=np.float32).copy()
        ret[2] = ret[0] + ret[2]
        ret[3] = ret[1] + ret[3]
        return ret

    @staticmethod
    def tlwh_to_xyah(tlwh):
        """
        tlwh: [x, y, w, h]  (top-left x,y)
        xyah: [cx, cy, a, h] where a = w / h
        """
        x, y, w, h = tlwh
        cx = x + w / 2.0
        cy = y + h / 2.0
        a = w / (h + 1e-6)
        return np.array([cx, cy, a, h], dtype=np.float32)

    @staticmethod
    def xyah_to_tlwh(xyah):
        """
        xyah: [cx, cy, a, h]
        tlwh: [x, y, w, h]
        """
        cx, cy, a, h = xyah
        w = a * h
        x = cx - w / 2.0
        y = cy - h / 2.0
        return np.array([x, y, w, h], dtype=np.float32)

    def activate(self, kalman_filter, frame_id):
        self.kalman_filter = kalman_filter
        self.track_id = self.next_id()
        self.mean, self.covariance = self.kalman_filter.initiate(
            self.tlwh_to_xyah(self._tlwh)
        )
        self.tracklet_len = 0
        self.state = TrackState.Tracked
        self.is_activated = True
        self.frame_id = frame_id
        self.start_frame = frame_id

    def re_activate(self, new_track, frame_id, new_id=False):
        self.mean, self.covariance = self.kalman_filter.update(
            self.mean, self.covariance,
            self.tlwh_to_xyah(new_track.tlwh)
        )
        self.tracklet_len = 0
        self.state = TrackState.Tracked
        self.is_activated = True
        self.frame_id = frame_id
        if new_id:
            self.track_id = self.next_id()

        # >>> DEPTH PATCH
        self.update_depth(new_track.depth)

    def update(self, new_track, frame_id):
        self.frame_id = frame_id
        self.tracklet_len += 1

        self.mean, self.covariance = self.kalman_filter.update(
            self.mean, self.covariance,
            self.tlwh_to_xyah(new_track.tlwh)
        )
        self.state = TrackState.Tracked
        self.is_activated = True

        self.score = new_track.score

        # >>> DEPTH PATCH
        self.update_depth(new_track.depth)

    # >>> DEPTH PATCH
    def update_depth(self, det_depth, alpha=0.8):
        if det_depth is None:
            return
        if not np.isfinite(det_depth):
            return
        if not np.isfinite(self.depth):
            self.depth = det_depth
        else:
            self.depth = alpha * self.depth + (1 - alpha) * det_depth

    @property
    def tlwh(self):
        if self.mean is None:
            return self._tlwh.copy()
        ret = self.mean[:4].copy()
        ret[2] *= ret[3]
        ret[:2] -= ret[2:] / 2
        return ret

    @property
    def tlbr(self):
        ret = self.tlwh
        ret[2:] += ret[:2]
        return ret


def apply_depth_gating(
    dists, tracks, detections,
    depth_gate=1.2,          # 基础“绝对门限”（中远距离用）
    depth_lambda=0.0,        # soft penalty
    rel_gate=0.22,           # 关键：相对门限（近距离会放宽绝对差）
    max_rate=0.55            # 关键：深度变化率门限（m/帧，按你深度尺度调）
):
    """
    - hard gate 采用 max( abs_gate, rel_gate * track_depth )
      近处 depth 小 -> abs_diff 更敏感，所以用相对门限更合理
    - 额外加入 depth_rate：允许近处快速靠近，但拒绝“跳层”式突变
    """
    if dists.size == 0:
        return dists

    INF = 1e5

    for i, t in enumerate(tracks):
        td = getattr(t, "depth", np.nan)
        if not np.isfinite(td):
            continue

        # 用 track 深度决定相对阈值：越近允许越大的“绝对深度差”
        # gate_abs = max(固定阈值, 相对阈值*当前深度)
        gate_abs = max(float(depth_gate), float(rel_gate) * float(td))

        # 速度阈值：time_since_update 越大，允许累计变化越多
        # （避免因为短暂丢帧导致 gate 太紧）
        tsu = getattr(t, "time_since_update", 0)
        allow = gate_abs + max_rate * max(1, tsu)

        for j, d in enumerate(detections):
            dd = getattr(d, "depth", np.nan)
            if not np.isfinite(dd):
                continue

            diff = abs(float(td) - float(dd))

            # hard gate
            if diff > allow:
                # print("GATE", td, dd, diff, allow)
                dists[i, j] = INF
                continue

            # soft penalty（建议很小）
            if depth_lambda > 0:
                dists[i, j] += depth_lambda * (diff / (allow + 1e-6))

    return dists



class BYTETracker:
    def __init__(self, args, frame_rate=30,
                 depth_gate=1.2,
                 depth_lambda=0.0,
                 reid_sim_thresh=0.65,
                 reid_depth_gate=1.8):
        self.tracked_stracks = []
        self.lost_stracks = []
        self.removed_stracks = []

        self.frame_id = 0
        self.args = args

        self.det_thresh = args.track_thresh
        self.buffer_size = int(frame_rate / 30.0 * args.track_buffer)
        self.max_time_lost = self.buffer_size

        self.kalman_filter = KalmanFilter()

        # depth
        self.depth_gate = depth_gate
        self.depth_lambda = depth_lambda

        # ReID (HSV hist)
        self.reid_sim_thresh = reid_sim_thresh
        self.reid_depth_gate = reid_depth_gate

        # appearance fusion for main matching
        self.w_app = getattr(args, "w_app", 0.35)  # 0.3~0.5
        self.app_gate = getattr(args, "app_gate", 0.85)  # cost>0.85 => sim<0.15 禁配（很宽松）

    def _reid_reconnect(self, det, img_bgr):
        """在 lost_stracks 里找最像的旧 track，像就接回原 ID。"""
        if img_bgr is None:
            return None

        det_feat = compute_hsv_hist(img_bgr, det.tlbr)
        if det_feat is None:
            return None

        best_t = None
        best_sim = self.reid_sim_thresh

        for t in self.lost_stracks:
            if getattr(t, "reid_feat", None) is None:
                continue

            # depth hard gate（防止接错前后人）
            td = getattr(t, "depth", np.nan)
            dd = getattr(det, "depth", np.nan)
            if np.isfinite(td) and np.isfinite(dd):
                if abs(float(td) - float(dd)) > float(self.reid_depth_gate):
                    continue

            sim = cosine_sim(det_feat, t.reid_feat)
            if sim > best_sim:
                best_sim = sim
                best_t = t

        # 给 det 保存一下特征，后续 activate 后也能用
        det.reid_feat = det_feat
        return best_t

    def _appearance_cost(self, tracks, detections):
        """
        返回 [len(tracks), len(detections)] 的 cost 矩阵，越小越像
        cost = 1 - max(0, cosine_sim)
        """
        T, D = len(tracks), len(detections)
        C = np.ones((T, D), dtype=np.float64)

        for i, t in enumerate(tracks):
            tf = getattr(t, "reid_feat", None)
            if tf is None:
                continue
            for j, d in enumerate(detections):
                df = getattr(d, "reid_feat", None)
                if df is None:
                    continue
                sim = cosine_sim(tf, df)  # [-1,1]
                sim = max(0.0, sim)  # clamp
                C[i, j] = 1.0 - sim  # [0,1]
        return C

    def update(self, dets, img_info, img_size, img_bgr=None, depths=None):
        """
        dets: Nx5  (x1,y1,x2,y2,score)
        depths: N   (float, may contain NaN)
        img_bgr: 当前帧 BGR 图像（用于 HSV ReID）
        """
        self.frame_id += 1

        activated_stracks = []
        refind_stracks = []
        lost_stracks_new = []
        removed_stracks_new = []

        if dets is None or len(dets) == 0:
            dets = np.zeros((0, 5), dtype=np.float64)

        if depths is None:
            depths = [np.nan] * len(dets)

        scores = dets[:, 4]
        bboxes = dets[:, :4]

        remain_inds = scores > self.det_thresh
        dets = dets[remain_inds]
        scores = scores[remain_inds]
        bboxes = bboxes[remain_inds]
        depths = np.asarray(depths)[remain_inds]

        # build detections
        detections = []
        for i in range(len(dets)):
            det = STrack(
                STrack.tlbr_to_tlwh(bboxes[i]),
                scores[i],
                depth=depths[i]
            )
            # 给 det 提前算 ReID 特征（如果有图像）
            if img_bgr is not None:
                det.reid_feat = compute_hsv_hist(img_bgr, det.tlbr)
            detections.append(det)

        # ====== 1) predict ======
        strack_pool = self.tracked_stracks + self.lost_stracks
        STrack.multi_predict(strack_pool)

        # ====== 2) iou + appearance + depth matching ======
        d_iou = matching.iou_distance(strack_pool, detections).astype(np.float32)  # [0,1]，越小越好
        d_app = app_distance(strack_pool, detections).astype(np.float32)  # [0,1]，越小越好

        # 1) 先做 appearance hard gate：明显不像的直接禁配（防止“抢ID”）
        INF = 1e5
        d_iou[d_app > self.app_gate] = INF

        # 2) 融合 cost（关键）
        w = float(self.w_app)
        dists = (1.0 - w) * d_iou + w * d_app

        # 3) 再做 depth gating（你已有的 hard gate + penalty）
        dists = apply_depth_gating(
            dists, strack_pool, detections,
            self.depth_gate, self.depth_lambda
        )

        matches, u_track, u_detection = matching.linear_assignment(
            dists, thresh=self.args.match_thresh
        )

        # ====== 3) matched ======
        for itracked, idet in matches:
            track = strack_pool[itracked]
            det = detections[idet]

            if track.state == TrackState.Tracked:
                track.update(det, self.frame_id)
                activated_stracks.append(track)
            else:
                track.re_activate(det, self.frame_id, new_id=False)
                refind_stracks.append(track)

            # 更新 ReID 特征（用当前 det 的）
            if getattr(det, "reid_feat", None) is not None:
                track.reid_feat = det.reid_feat

        # ====== 4) unmatched tracks -> lost ======
        for it in u_track:
            track = strack_pool[it]
            if track.state == TrackState.Tracked:
                track.mark_lost()
                lost_stracks_new.append(track)

        # ====== 5) unmatched detections: 先尝试 ReID 接回 lost，否则 new id ======
        for idet in u_detection:
            det = detections[idet]

            # 尝试 reid 连接到 lost
            t = self._reid_reconnect(det, img_bgr)
            if t is not None:
                t.re_activate(det, self.frame_id, new_id=False)
                activated_stracks.append(t)
                if t in self.lost_stracks:
                    self.lost_stracks.remove(t)
                # 更新 reid_feat
                if getattr(det, "reid_feat", None) is not None:
                    t.reid_feat = det.reid_feat
                continue

            # 真正 new id
            det.activate(self.kalman_filter, self.frame_id)
            # 新 track 的 reid_feat 已经在上面算了
            activated_stracks.append(det)

        # ====== 6) 清理 lost 太久的 ======
        # 保留历史 lost，再加上本帧新 lost
        self.lost_stracks.extend(lost_stracks_new)

        # 清理超时
        kept_lost = []
        for t in self.lost_stracks:
            if self.frame_id - t.end_frame <= self.max_time_lost:
                kept_lost.append(t)
            else:
                t.mark_removed()
                removed_stracks_new.append(t)
        self.lost_stracks = kept_lost

        # tracked 更新：本帧激活 + re-find
        self.tracked_stracks = activated_stracks + refind_stracks
        # 只保留本次 update 新增 removed 的 track，避免历史对象长期挂在 tracker 上。
        self.removed_stracks = removed_stracks_new

        return self.tracked_stracks
