from typing import Dict
import warnings

import base64
import cv2
import io
import numpy as np
from PIL import Image
import zlib


class EarlyStopping:
    def __init__(self,
                 monitor_metric: str,
                 patience: int = 10,
                 min_delta: float = 0.01):
        assert min_delta >= 0, 'min_delta must be non-negative'
        assert patience >= 0, 'patience must be non-negative'
        assert monitor_metric is not None, 'monitor metric should not be None'

        self.monitor_metric = monitor_metric
        self.patience = patience
        self.min_delta = min_delta
        self.optimal_value, self.mode = (np.inf, 'min') if 'loss' in monitor_metric else (-np.inf, 'max')
        self.counter = 0
        self.early_stop = False

    def __call__(self, metrics: Dict[str, float]):
        score = metrics.get(self.monitor_metric)
        assert score is not None, '{} doesn\'t exist in metrics'.format(self.monitor_metric)

        if self.is_better_optimum(score):
            self.counter = 0
            self.optimal_value = score
        else:
            self.counter += 1

        if self.counter >= self.patience:
            self.early_stop = True

    def is_better_optimum(self, score):
        if self.mode == 'max':
            if score > self.optimal_value and (abs(score - self.optimal_value) > self.min_delta):
                return True
            else:
                return False
        if self.mode == 'min':
            if score < self.optimal_value and (abs(score - self.optimal_value) > self.min_delta):
                return True
            else:
                return False


class BalancedWeighting:
    def __init__(self, alpha: float = 0.5):
        self.w1 = 0
        self.w2 = 0
        self.alpha = alpha
        self.first_batch_loss1 = None
        self.first_batch_loss2 = None
        self.current_loss1 = None
        self.current_loss2 = None
        self.first_batch_initialized = False

    def init_current_losses(self, loss1, loss2):
        self.current_loss1 = loss1
        self.current_loss2 = loss2

    def init_first_batch_losses(self, loss1, loss2):
        if not self.first_batch_initialized:
            self.first_batch_loss1 = loss1
            self.first_batch_loss2 = loss2
            self.first_batch_initialized = True

    def get_weights(self):
        self.w1 = (self.current_loss1 / self.first_batch_loss1) ** self.alpha
        self.w2 = (self.current_loss2 / self.first_batch_loss2) ** self.alpha
        return self.w1, self.w2

    def batch_update(self, loss_seg_np, loss_cls_np):
        self.init_first_batch_losses(loss_seg_np, loss_cls_np)
        self.init_current_losses(loss_seg_np, loss_cls_np)

    def end_of_iteration(self):
        self.first_batch_initialized = False


class StaticWeighting:
    def __init__(self, w1=0.55, w2=0.45):
        self.w1 = w1
        self.w2 = w2

    def get_weights(self):
        return self.w1, self.w2

    def end_of_iteration(self):
        pass

    def batch_update(self, loss_seg_np, loss_cls_np):
        pass


def binary_search(img: np.array, threshold_start: int, threshold_end: int, optimal_area: float):
    assert len(img.shape) == 2, 'invalid shape'
    best_value = None
    while threshold_start <= threshold_end:
        mid = (threshold_start + threshold_end) // 2
        cut_img = img[:, :mid]
        cut_img_area = np.sum(cut_img)
        best_value = mid

        if cut_img_area <= optimal_area:
            threshold_start = mid + 1
        if cut_img_area > optimal_area:
            threshold_end = mid - 1
    return best_value


def separate_lungs(mask: np.array):
    assert np.max(mask) <= 1 and np.min(mask) >= 0, 'mask values should be in [0,1] scale, max {}' \
                                                    ' min {}'.format(np.max(mask), np.min(mask))
    binary_map = (mask > 0.5).astype(np.uint8)
    num_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(binary_map, connectivity=8, ltype=cv2.CV_32S)
    centroids = centroids.astype(np.int32)
    lungs = []

    if num_labels != 3:
        warnings.warn('There aren\'t 2 objects on predicted mask, this might cause incorrect results')
        stats = np.append(stats, [stats[-1]], axis=0)
        centroids = np.append(centroids, [centroids[-1]], axis=0)

    for i in range(1, 3):
        x0, y0 = stats[i, cv2.CC_STAT_LEFT], stats[i, cv2.CC_STAT_TOP]
        x1, y1 = x0 + stats[i, cv2.CC_STAT_WIDTH], y0 + stats[i, cv2.CC_STAT_HEIGHT]
        zero_matrix = np.zeros_like(mask)
        zero_matrix[y0:y1, x0:x1] = mask[y0:y1, x0:x1]
        lungs.append({'lung': zero_matrix, 'centroid': centroids[i]})

    if lungs[0]['centroid'][0] < lungs[1]['centroid'][0]:
        left_lung, right_lung = lungs[0]['lung'], lungs[1]['lung']
    else:
        right_lung, left_lung = lungs[0]['lung'], lungs[1]['lung']
    return left_lung, right_lung


def split_lung_into_segments(lung: np.array):
    rotated_lung = cv2.rotate(lung, cv2.ROTATE_90_CLOCKWISE)
    height, width = rotated_lung.shape

    thr_1 = binary_search(rotated_lung.copy(), 0, width, np.sum(lung) // 3)
    thr_2 = binary_search(rotated_lung.copy(), 0, width, 2 * np.sum(lung) // 3)

    pad_1 = np.pad(rotated_lung[:, :thr_1], [(0, 0), (0, width - thr_1)], mode='constant', constant_values=0)
    pad_2 = np.pad(rotated_lung[:, thr_1:thr_2], [(0, 0), (thr_1, width - thr_2)], mode='constant', constant_values=0)
    pad_3 = np.pad(rotated_lung[:, thr_2:], [(0, 0), (thr_2, 0)], mode='constant', constant_values=0)

    img1 = cv2.rotate(pad_1, cv2.ROTATE_90_COUNTERCLOCKWISE)
    img2 = cv2.rotate(pad_2, cv2.ROTATE_90_COUNTERCLOCKWISE)
    img3 = cv2.rotate(pad_3, cv2.ROTATE_90_COUNTERCLOCKWISE)

    return img1, img2, img3


def find_obj_bbox(mask: np.array):
    assert np.max(mask) <= 1 and np.min(mask) >= 0, 'mask values should be in [0,1] scale, max {}' \
                                                    ' min {}'.format(np.max(mask),  np.min(mask))
    binary_map = (mask > 0.5).astype(np.uint8)
    num_labels, _, stats, _ = cv2.connectedComponentsWithStats(binary_map, connectivity=8, ltype=cv2.CV_32S)
    bbox_coordinates = []

    for i in range(1, num_labels):
        x0, y0 = stats[i, cv2.CC_STAT_LEFT], stats[i, cv2.CC_STAT_TOP]
        x1, y1 = x0 + stats[i, cv2.CC_STAT_WIDTH], y0 + stats[i, cv2.CC_STAT_HEIGHT]
        bbox_coordinates.append((x0, y0, x1, y1))
    return bbox_coordinates


def build_smp_model_from_path(model_path):
    models = ['Unet', 'Unet++', 'DeepLabV3', 'DeepLabV3+', 'FPN', 'Linknet', 'PSPNet', 'PAN']

    encoders = ['resnet18', 'resnet34', 'resnet50', 'resnet101', 'resnet152', "resnext50_32x4d", "resnext101_32x4d",
                'resnext101_32x8d', 'resnext101_32x16d', 'resnext101_32x32d', 'resnext101_32x48d', 'timm-resnest14d',
                'timm-resnest26d', "timm-resnest50d", "timm-resnest101e", "timm-resnest200e", "timm-resnest269e",
                "timm-resnest50d_4s2x40d",
                "timm-resnest50d_1s4x24d", "timm-res2net50_26w_4s", "timm-res2net101_26w_4s", "timm-res2net50_26w_8s",
                "timm-res2net50_48w_2s", "timm-res2net50_14w_8s", "timm-res2next50", "timm-regnetx_016",
                "timm-regnetx_032",
                "timm-res2net50_26w_6s", "timm-regnetx_002", "timm-regnetx_004", "timm-regnetx_006", "timm-regnetx_008",
                "timm-regnetx_040", "timm-regnetx_064", "timm-regnetx_080", "timm-regnetx_120", "timm-regnetx_160",
                "timm-regnetx_320", "timm-regnety_002", "timm-regnety_004", "timm-regnety_006", "timm-regnety_008",
                "timm-regnety_016",
                "timm-regnety_032", "timm-regnety_040", "timm-regnety_064", "timm-regnety_080", "timm-regnety_120",
                "timm-regnety_160", "timm-regnety_320", "senet154", "se_resnet50", "se_resnet101", "se_resnet152",
                "se_resnext50_32x4d", "se_resnext101_32x4d", "timm-skresnet18", "timm-skresnet34",
                "timm-skresnext50_32x4d",
                "densenet121", "densenet169", "densenet201", "densenet161", "inceptionresnetv2", "inceptionv4",
                "xception",
                "efficientnet-b0", "efficientnet-b1", "efficientnet-b2", "efficientnet-b3", "efficientnet-b4",
                "efficientnet-b5",
                "efficientnet-b6", "efficientnet-b7", "timm-efficientnet-b0", "timm-efficientnet-b1",
                "timm-efficientnet-b2",
                "timm-efficientnet-b3", "timm-efficientnet-b4", "timm-efficientnet-b5", "timm-efficientnet-b6",
                "timm-efficientnet-b7", "timm-efficientnet-b8", "timm-efficientnet-l2", "timm-efficientnet-lite0",
                "timm-efficientnet-lite1", "timm-efficientnet-lite2", "timm-efficientnet-lite3",
                "timm-efficientnet-lite4",
                "mobilenet_v2", "dpn68", "dpn68b", "dpn92", "dpn98", "dpn107", "dpn131", "vgg11", "vgg11_bn", "vgg13",
                "vgg13_bn", "vgg16", "vgg16_bn", "vgg19", "vgg19_bn"
                ]

    weights = ['imagenet', 'ssl', 'swsl', 'instagram', 'imagenet+background', 'noisy-student', 'advprop', 'imagenet+5k']
    built_model = {'model_name': None, 'encoder_name': None, 'encoder_weights': None}
    flag = False
    for model in models:
        if model + '_' in model_path:
            if flag:
                warnings.warn('The occurred error is related to the model building (models). This may cause problems!')
            flag = True
            built_model['model_name'] = model

    if not flag:
        warnings.warn('Automatic parser didn\'t find model_name')

    model_path = model_path.replace(built_model['model_name'] + '_', '*')

    flag = False
    for encoder in encoders:
        if '*' + encoder + '_' in model_path:
            if flag:
                warnings.warn('The occurred error is related to the model building (encoders). This may cause problems!')
            flag = True
            built_model['encoder_name'] = encoder

    if not flag:
        warnings.warn('Automatic parser didn\'t find encoder_name')

    flag = False
    for weight in weights:
        if '_' + weight + '_' in model_path:
            if flag:
                warnings.warn('The occurred error is related to the model building (weights). This may cause problems!')
            flag = True
            built_model['encoder_weights'] = weight

    if not flag:
        warnings.warn('Automatic parser didn\'t find encoder_weights')

    return built_model


def mask_2_base64(mask):
    img_pil = Image.fromarray(np.array(mask, dtype=np.uint8))
    img_pil.putpalette([0, 0, 0, 255, 255, 255])
    bytes_io = io.BytesIO()
    img_pil.save(bytes_io, format='PNG', transparency=0, optimize=0)
    bytes = bytes_io.getvalue()
    return base64.b64encode(zlib.compress(bytes)).decode('utf-8')


def base64_to_image(s: str) -> np.ndarray:
    z = zlib.decompress(base64.b64decode(s))
    n = np.frombuffer(z, np.uint8)

    img_decoded = cv2.imdecode(n, cv2.IMREAD_UNCHANGED)
    if (len(img_decoded.shape) == 3) and (img_decoded.shape[2] >= 4):
        mask = img_decoded[:, :, 3].astype(np.uint8)  # 4-channel images
    elif len(img_decoded.shape) == 2:
        mask = img_decoded.astype(np.uint8)  # flat 2D mask
    else:
        raise RuntimeError('Wrong internal mask format.')
    return mask


def filter_img(img: np.array, contour_area: int = 6000):
    '''this applys morphological filtering to image, but at first it removes small unnecessary objects'''

    thresh = (img > 0.5).astype(np.uint8)
    cnts = cv2.findContours(thresh, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
    cnts = cnts[0] if len(cnts) == 2 else cnts[1]
    for c in cnts:
        area = cv2.contourArea(c)
        if area < contour_area:
            cv2.drawContours(thresh, [c], -1, (0, 0, 0), -1)

    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    opening = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, kernel, iterations=2)
    closing = cv2.morphologyEx(opening, cv2.MORPH_CLOSE, kernel)
    return closing