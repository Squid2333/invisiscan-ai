from __future__ import annotations

import os
from concurrent import futures

import grpc
from google.protobuf.timestamp_pb2 import Timestamp

from invisiscan.v1 import common_pb2, perception_pb2, perception_pb2_grpc


def mock_detections(seq: int, width: int, height: int):
    # 最初两帧的检测打开一个未确认事件。下一事件中左侧轨迹出现得更早，
    # 但中途漏了几帧，因此比右侧轨迹更晚确认。
    left_depths = {23: float("nan"), 27: 2.5, 28: 3.5, 29: float("nan"), 30: 4.2}
    if seq in left_depths:
        lane, score, depth = 0.10, 0.90, left_depths[seq]
    elif seq in (1, 2, 24, 25, 26):
        lane, score, depth = 0.60, 0.90, 3.0
    elif seq == 50:
        # 前面 19 个空帧还不足以关闭事件。这一帧的低分框会被跟踪器丢弃，
        # 但事件状态机仍会把缺席计数清零。
        lane, score, depth = 0.10, 0.08, 3.0
    else:
        return []

    return [
        perception_pb2.Detection(
            box=common_pb2.BBox(
                x1=(lane + 0.75 / 960) * width,
                y1=(0.20 + 0.75 / 720) * height,
                x2=(lane + 0.20 + 1.25 / 960) * width,
                y2=(0.60 + 1.25 / 720) * height,
            ),
            score=score,
            depth=depth,
        )
    ]


class MockPerceptionService(perception_pb2_grpc.PerceptionServiceServicer):
    def Perceive(self, request_iterator, context):
        effective = None
        for request in request_iterator:
            payload = request.WhichOneof("payload")
            if payload == "config":
                config = request.config
                roi = common_pb2.BBox(x1=800, y1=1000, x2=1200, y2=1100)
                if config.HasField("roi"):
                    roi.CopyFrom(config.roi)
                effective = perception_pb2.SessionConfig(
                    stream_id=config.stream_id,
                    max_side=960,
                    depth_input_size=384,
                    det_conf=0.05,
                    det_iou=0.8,
                    roi=roi,
                    roi_percentile=50,
                    default_depth_threshold=2.5,
                    gate_offset=0.4,
                    depth_filter_mode=perception_pb2.DEPTH_FILTER_MODE_GE,
                    nms_iou_close=0.3,
                    nms_iou_far=0.6,
                    nms_depth_delta=0.7,
                    gmc_method="sparseOptFlow",
                    with_reid=True,
                    gmc_det_score_thresh=0.1,
                )
                yield perception_pb2.PerceiveResponse(
                    ready=perception_pb2.SessionReady(
                        effective_config=effective,
                        detector_model="mock-detector",
                        depth_model="mock-depth",
                        reid_dim=0,
                    )
                )
            elif payload == "frame":
                if effective is None:
                    context.abort(
                        grpc.StatusCode.FAILED_PRECONDITION,
                        "send SessionConfig before frames",
                    )
                frame = request.frame
                timestamp = Timestamp()
                if frame.HasField("frame_time"):
                    timestamp.CopyFrom(frame.frame_time)
                width, height = frame.image.width, frame.image.height
                scale = min(1.0, effective.max_side / max(width, height))
                infer_width = max(1, round(width * scale))
                infer_height = max(1, round(height * scale))
                yield perception_pb2.PerceiveResponse(
                    result=perception_pb2.FrameResult(
                        frame_seq=frame.frame_seq,
                        frame_time=timestamp,
                        infer_width=infer_width,
                        infer_height=infer_height,
                        detections=mock_detections(frame.frame_seq, infer_width, infer_height),
                        gmc_warp=[1.0, 0.0, 0.0, 0.0, 1.0, 0.0],
                        depth_gate=2.5,
                        roi_depth_valid=False,
                        timings=common_pb2.Timings(total_ms=0),
                    )
                )
            else:
                context.abort(grpc.StatusCode.INVALID_ARGUMENT, "request payload is required")

    def ClassifyTrajectory(self, request, context):
        return perception_pb2.ClassifyTrajectoryResponse(
            category="pass",
            confidence=0.0,
            probs={"indoor": 0.0, "outdoor": 0.0, "pass": 1.0},
            steps=0,
            model="mock-classifier",
        )

    def SegmentBodies(self, request, context):
        return perception_pb2.SegmentBodiesResponse(
            results=[perception_pb2.SegmentResult(mask_area=0.0) for _ in request.images]
        )


def serve() -> None:
    max_message_bytes = int(os.environ.get("GRPC_MAX_MESSAGE_BYTES", "16777216"))
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=4),
        options=[
            ("grpc.max_receive_message_length", max_message_bytes),
            ("grpc.max_send_message_length", max_message_bytes),
        ],
    )
    perception_pb2_grpc.add_PerceptionServiceServicer_to_server(
        MockPerceptionService(), server
    )
    address = os.environ.get("INVISISCAN_PERCEPTION_LISTEN", "127.0.0.1:50051")
    if server.add_insecure_port(address) == 0:
        raise RuntimeError(f"could not bind PerceptionService to {address}")
    server.start()
    server.wait_for_termination()


if __name__ == "__main__":
    serve()
