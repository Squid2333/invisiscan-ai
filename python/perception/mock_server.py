from __future__ import annotations

import os
from concurrent import futures

import grpc
from google.protobuf.timestamp_pb2 import Timestamp

from invisiscan.v1 import common_pb2, perception_pb2, perception_pb2_grpc


class MockPerceptionService(perception_pb2_grpc.PerceptionServiceServicer):
    def Perceive(self, request_iterator, context):
        configured = False
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
                configured = True
                yield perception_pb2.PerceiveResponse(
                    ready=perception_pb2.SessionReady(
                        effective_config=effective,
                        detector_model="mock-detector",
                        depth_model="mock-depth",
                        reid_dim=0,
                    )
                )
            elif payload == "frame":
                if not configured:
                    context.abort(
                        grpc.StatusCode.FAILED_PRECONDITION,
                        "send SessionConfig before frames",
                    )
                frame = request.frame
                timestamp = Timestamp()
                if frame.HasField("frame_time"):
                    timestamp.CopyFrom(frame.frame_time)
                yield perception_pb2.PerceiveResponse(
                    result=perception_pb2.FrameResult(
                        frame_seq=frame.frame_seq,
                        frame_time=timestamp,
                        infer_width=960,
                        infer_height=540,
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
