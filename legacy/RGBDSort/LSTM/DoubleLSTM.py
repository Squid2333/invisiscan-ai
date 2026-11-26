import numpy as np
import torch
import json
import os
from torch import nn
from torch.nn.utils.rnn import pack_padded_sequence


# -------------------------
# 1) 预处理：xywhd,H,W -> [x_n,y_n,w_n,h_n,d_n,vx,vy,dd]
# -------------------------
def preprocess_xywhd_lines(
    lines,
    *,
    xy_is_center: bool = True,
    ema_alpha: float = 0.8,
    d_p1: float = None,
    d_p99: float = None,
    clip_v: float = 0.2,
    clip_dd: float = 0.2
) -> np.ndarray:
    seq = []

    prev_xn = prev_yn = None
    prev_dn = None
    prev_d_ema = None

    for line in lines:
        parts = line.strip().split(',')
        if len(parts) < 7:
            continue

        x = float(parts[0]); y = float(parts[1])
        w = float(parts[2]); h = float(parts[3])
        d = float(parts[4])
        H = float(parts[5]); W = float(parts[6])

        # x,y 是否中心点
        if not xy_is_center:
            cx = x + 0.5 * w
            cy = y + 0.5 * h
        else:
            cx, cy = x, y

        # 归一化 xywh
        xn = cx / (W + 1e-8)
        yn = cy / (H + 1e-8)
        wn = w  / (W + 1e-8)
        hn = h  / (H + 1e-8)

        # 深度 EMA
        if prev_d_ema is None:
            d_ema = d
        else:
            d_ema = ema_alpha * prev_d_ema + (1.0 - ema_alpha) * d
        prev_d_ema = d_ema

        # 深度归一化（与训练一致）
        if d_p1 is not None and d_p99 is not None and (d_p99 - d_p1) != 0:
            dn = (d_ema - d_p1) / (d_p99 - d_p1)
            dn = float(np.clip(dn, 0.0, 1.0))
        else:
            dn = float(d_ema)

        # 差分
        if prev_xn is None:
            vx = 0.0; vy = 0.0; dd = 0.0
        else:
            vx = xn - prev_xn
            vy = yn - prev_yn
            dd = dn - prev_dn

        vx = float(np.clip(vx, -clip_v, clip_v))
        vy = float(np.clip(vy, -clip_v, clip_v))
        dd = float(np.clip(dd, -clip_dd, clip_dd))

        prev_xn, prev_yn, prev_dn = xn, yn, dn
        seq.append([xn, yn, wn, hn, dn, vx, vy, dd])

    if len(seq) == 0:
        return np.zeros((0, 8), dtype=np.float32)

    return np.asarray(seq, dtype=np.float32)


# -------------------------
# 2) pack 版模型（forward 需要 lengths）
# -------------------------
class DoubleLSTM_Packed(nn.Module):
    def __init__(self, input_size, hidden_size, num_classes):
        super().__init__()
        self.hidden_size = hidden_size
        self.lstm1 = nn.LSTM(input_size, hidden_size, batch_first=True)
        self.lstm2 = nn.LSTM(hidden_size, hidden_size, batch_first=True)
        self.fc = nn.Linear(hidden_size, num_classes)

    def forward(self, X, lengths):
        """
        X: [B, Lmax, input_size] (padding 过也可以)
        lengths: [B]
        """
        lengths_sorted, idx = lengths.sort(descending=True)
        X_sorted = X[idx]

        packed1 = pack_padded_sequence(X_sorted, lengths_sorted.cpu(), batch_first=True, enforce_sorted=True)
        packed_out1, _ = self.lstm1(packed1)

        packed_out2, (h_n, _) = self.lstm2(packed_out1)

        # 最后一层最后时刻 hidden
        last = h_n[-1]  # [B, H]

        # 还原 batch 原顺序
        _, inv = idx.sort()
        last = last[inv]

        return self.fc(last)  # [B, C]


DEFAULT_CLASS_NAMES = ["indoor", "outdoor"]


def _infer_num_classes_from_state(state):
    fc_weight = state.get("fc.weight")
    if fc_weight is None:
        raise KeyError("state_dict missing fc.weight, cannot infer num_classes")
    return int(fc_weight.shape[0])


def _resolve_labels_path(model_path, labels_path=None):
    if labels_path:
        return labels_path
    model_dir = os.path.dirname(os.path.abspath(model_path))
    return os.path.join(model_dir, "lstm_labels.json")


def load_label_names(labels_path, num_classes):
    if os.path.isfile(labels_path):
        with open(labels_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        idx_to_category = payload.get("idx_to_category")
        if isinstance(idx_to_category, list) and len(idx_to_category) == num_classes:
            return list(idx_to_category)

    if num_classes == len(DEFAULT_CLASS_NAMES):
        return list(DEFAULT_CLASS_NAMES)
    return list(DEFAULT_CLASS_NAMES) + [f"class_{idx}" for idx in range(len(DEFAULT_CLASS_NAMES), num_classes)]


def load_LSTMmodel(model_path, device='cuda', labels_path=None):
    input_size = 8
    hidden_size = 256
    state = torch.load(model_path, map_location=device)
    num_classes = _infer_num_classes_from_state(state)
    class_names = load_label_names(_resolve_labels_path(model_path, labels_path), num_classes)

    model = DoubleLSTM_Packed(input_size, hidden_size, num_classes).to(device)
    model.load_state_dict(state)
    model.class_names = class_names
    model.eval()
    return model


# -------------------------
# 3) 变长推理：不插值，直接 pack
# -------------------------
@torch.no_grad()
def lstm_predict_varlen(
    model,
    lines,
    confidence_threshold=0.9,
    d_p1=None,
    d_p99=None,
    xy_is_center=True,
    return_details=False,
):
    class_names = getattr(model, "class_names", DEFAULT_CLASS_NAMES)

    seq = preprocess_xywhd_lines(
        lines,
        xy_is_center=xy_is_center,
        ema_alpha=0.8,
        d_p1=d_p1,
        d_p99=d_p99,
        clip_v=0.2,
        clip_dd=0.2
    )

    # 至少2帧才有意义
    if seq.shape[0] < 15:
        if return_details:
            class_probs = {name: 0.0 for name in class_names}
            return "pass", 0.0, class_probs
        return "pass"

    device = next(model.parameters()).device

    # 单条序列也按 batch 方式喂：X [1,L,8]，lengths [1]
    X = torch.tensor(seq, dtype=torch.float32, device=device).unsqueeze(0)
    lengths = torch.tensor([seq.shape[0]], dtype=torch.long, device=device)

    logits = model(X, lengths)
    probs = torch.softmax(logits, dim=1)
    max_prob, pred = probs.max(dim=1)
    max_prob_value = float(max_prob.item())
    probs_list = probs.squeeze(0).detach().cpu().tolist()
    class_probs = {
        class_names[idx] if 0 <= idx < len(class_names) else f"class_{idx}": float(prob)
        for idx, prob in enumerate(probs_list)
    }

    if max_prob_value < confidence_threshold:
        if return_details:
            return "pass", max_prob_value, class_probs
        return "pass"

    pred_idx = int(pred.item())
    prediction = class_names[pred_idx] if 0 <= pred_idx < len(class_names) else f"class_{pred_idx}"
    if return_details:
        return prediction, max_prob_value, class_probs
    return prediction


# -------------------------
# 4) demo
# -------------------------
if __name__ == "__main__":
    lines = [
        "744,1266,136,283,3.3167,1440,2560",
        "744,1266,136,286,3.3245,1440,2560",
    ]

    norm = np.load("lstm_norm.npz")
    d_p1 = float(norm["d_p1"])
    d_p99 = float(norm["d_p99"])
    if np.isnan(d_p1) or np.isnan(d_p99):
        d_p1 = d_p99 = None

    # 这里换成你训练保存出来的权重文件，比如 lstm_pack.pth
    model = load_LSTMmodel("lstm_pack.pth", device="cuda")

    pred = lstm_predict_varlen(
        model, lines,
        confidence_threshold=0.9,
        d_p1=d_p1, d_p99=d_p99,
        xy_is_center=False  # 你目前写 False，我保留
    )
    print(pred)
