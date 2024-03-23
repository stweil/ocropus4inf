# sourcery skip: use-fstring-for-concatenation
import os
import os, random, shutil
import matplotlib.pyplot as plt
import numpy as np
import requests
import scipy.ndimage as ndi
import torch
import urllib
from typing import List, Tuple, Union

from collections import defaultdict

from . import nlbin

plt.rc("image", cmap="gray")
plt.rc("image", interpolation="nearest")

default_device = "?cuda:0" if torch.cuda.is_available() else "cpu"
default_device = os.environ.get("OCROPUS4_DEVICE", default_device)

cache_dir = os.path.expanduser(os.environ.get("OCROPUS4_CACHE", "~/.cache/ocropus4"))
model_bucket = "http://storage.googleapis.com/ocro-models/v1/"
default_textmodel = model_bucket + "lstm_resnet_v2-default.jit"
default_segmodel = model_bucket + "seg_unet_v2-default.jit"

class DefaultCharset:
    def __init__(self, chars="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"):
        if isinstance(chars, str):
            chars = list(chars)
        self.chars = [""] + chars

    def __len__(self):
        return len(self.chars)

    def encode_char(self, c):
        try:
            index = self.chars.index(c)
        except ValueError:
            index = len(self.chars) - 1
        return max(index, 1)

    def encode(self, s):
        assert isinstance(s, str)
        return [self.encode_char(c) for c in s]

    def decode(self, l):
        assert isinstance(l, list)
        return "".join([self.chars[k] for k in l])


def ctc_decode(probs, sigma=1.0, threshold=0.7, kind=None, full=False):
    """A simple decoder for CTC-trained OCR recognizers.

    :probs: d x l sequence classification output
    """
    assert probs.ndim == 2, probs.shape
    assert isinstance(probs, np.ndarray)
    probs = probs.T
    assert (
        abs(probs.sum(1) - 1) < 1e-4
    ).all(), f"input not normalized; did you apply .softmax()? {probs.sum(1)}"
    probs = ndi.gaussian_filter(probs, (sigma, 0))
    probs /= probs.sum(1)[:, np.newaxis]
    labels, n = ndi.label(probs[:, 0] < threshold)
    mask = np.tile(labels[:, np.newaxis], (1, probs.shape[1]))
    mask[:, 0] = 0
    maxima = ndi.maximum_position(probs, mask, np.arange(1, np.amax(mask) + 1))
    if not full:
        return [c for r, c in sorted(maxima)]
    else:
        return [(r, c, probs[r, c]) for r, c in sorted(maxima)]


def jit_change_device(model, device="cuda:0"):
    if not hasattr(model, "modules"):
        return
    for m in model.modules():
        if not hasattr(m, "original_name"):
            continue
        if m.original_name == "AutoDevice":
            m.device = "cuda:0"


class OnDevice:
    """Performs inference on device.

    The device string can be any valid device string for PyTorch.
    If it starts with a "?", the model is moved to the device before
    inference and moved back to the CPU afterwards.
    """

    def __init__(self, model, device):
        if device is None:
            self.device = device
            self.unload = False
        elif isinstance(device, str):
            if device.startswith("?mps"):
                device = device[1:]
            self.unload = device[0] == "?"
            self.device = device.strip("?")
        else:
            self.unload = False
            self.device = device
        self.model = model

    def __call__(self, inputs):
        if "cuda" not in self.device:
            print("warning: running on CPU")
        return self.model(inputs.to(self.device))

    def __enter__(self, *args):
        if self.device is not None:
            self.model = self.model.to(self.device)
            jit_change_device(self.model, self.device)
        return self

    def __exit__(self, *args):
        if self.device is not None and self.unload:
            self.model = self.model.to("cpu")


def usm_filter(image):
    return image - ndi.gaussian_filter(image, 16.0)


def remove_small_connected_components(image, threshold):
    labels, n = ndi.label(image)
    sizes = np.bincount(labels.ravel())
    mask_sizes = sizes > threshold
    mask_sizes[0] = 0
    remove_small = mask_sizes[labels]
    return remove_small


def spread_labels(labels, maxdist=9999999):
    """Spread the given labels to the background"""
    distances, features = ndi.distance_transform_edt(labels == 0, return_distances=1, return_indices=1)
    indexes = features[0] * labels.shape[1] + features[1]
    spread = labels.ravel()[indexes.ravel()].reshape(*labels.shape)
    spread *= distances < maxdist
    return spread


def remove_unmarked_regions(markers, regions):
    """Remove regions that are not marked by markers."""
    m = 1000000
    labels, _ = ndi.label(markers)
    rlabels, rn = ndi.label(regions)
    corr = np.unique((rlabels * m + labels).ravel())
    remap = np.zeros(rn + 1, dtype=np.int32)
    for k in corr:
        remap[k // m] = k % m
    return remap[rlabels]


def marker_segmentation(markers, regions, maxdist=100):
    regions = np.maximum(regions, markers)
    labels, _ = ndi.label(markers)
    regions = (remove_unmarked_regions(markers, regions) > 0)
    spread = spread_labels(labels, maxdist=maxdist)
    segmented = np.where(np.maximum(markers, regions), spread, 0)
    return segmented


class PageSegmenter:
    def __init__(self, murl, device=default_device):
        murl = murl or default_segmodel
        self.model = get_model(murl)
        self.device = device

    def inference(self, image):
        assert isinstance(image, np.ndarray)
        # print("segmenter:", np.amin(image), np.median(image), np.mean(image), np.amax(image))
        if image.ndim == 3:
            assert image.shape[2] in [1, 3, 4], image.shape
            image = np.mean(image[:, :, :3], axis=2)
        assert np.amin(image) >= 0
        assert np.amax(image) <= 1
        image = usm_filter(image)
        h, w = image.shape
        h32, w32 = ((h + 31) // 32) * 32, ((w + 31) // 32) * 32
        input = torch.zeros((h32, w32)).unsqueeze(0).unsqueeze(0)
        input[:, :, :h, :w] = torch.tensor(image)
        with OnDevice(self.model, self.device) as model:
            with torch.no_grad():
                output = model(input)
        if output.shape[1] == 7:
            probs = output.detach().sigmoid()[0].cpu().permute(1, 2, 0).numpy()
        elif output.shape[1] == 4:
            probs = output.detach().softmax(1)[0].cpu().permute(1, 2, 0).numpy()
        else:
            raise ValueError(f"bad output shape: {output.shape}")
        return probs


def batch_images(images, maxheight=48.0):
    images = [torch.tensor(im) if not torch.is_tensor(im) else im for im in images]
    images = [im.unsqueeze(0) if im.ndim == 2 else im for im in images]
    d, h, w = map(max, zip(*[x.shape for x in images]))
    assert h <= maxheight, [im.shape for im in images]
    result = torch.zeros((len(images), d, h, w), dtype=torch.float32)
    for i, im in enumerate(images):
        d, h, w = im.shape
        if im.dtype == torch.uint8:
            im = im.float() / 255.0
        result[i, :d, :h, :w] = im
    return result


def make_ascii_charset():
    chars = [chr(i) for i in range(32, 127)]
    charset = DefaultCharset(chars)
    return charset


def scale_to_maxheight(image, maxheight=48.0):
    assert isinstance(image, np.ndarray)
    assert image.ndim == 2, image.shape
    h, w = image.shape
    scale = float(maxheight) / h
    if scale >= 1.0:
        return image
    return ndi.zoom(image, scale, order=1)


def get_model(url):
    # parse the path as a url
    scheme, netloc, path, params, query, fragment = urllib.parse.urlparse(url)
    # if the scheme is file or empty, then it is a local file
    if scheme in ["", "file"]:
        return load_model(path)
    elif scheme in ["http", "https"]:
        # download the file to $HOME/.cache/ocropus4
        os.makedirs(cache_dir, exist_ok=True)
        fname = os.path.basename(path)
        local = os.path.join(cache_dir, fname)
        if not os.path.exists(local):
            print("downloading", url, "to", local)
            with open(local, "wb") as stream:
                stream.write(requests.get(url).content)
        return load_model(local)
    elif scheme in ["gs"]:
        # download using the gsutil command line program
        os.makedirs(cache_dir, exist_ok=True)
        fname = os.path.basename(path)
        local = os.path.join(cache_dir, fname)
        if not os.path.exists(local):
            print("downloading", url, "to", local)
            os.system(f"gsutil cp {url} {local}")
        return load_model(local)
    else:
        raise ValueError(f"unknown url scheme: {url}")
    
def flatten_parameters(model):
    for m in model.modules():
        if hasattr(m, "flatten_parameters"):
            m.flatten_parameters()
    return model

def load_model(path):
    print("loading model", path)
    if path.endswith(".jit"):
        import torch.jit

        model = torch.jit.load(path, map_location=torch.device("cpu"))
        model = flatten_parameters(model)
        return model
    elif path.endswith(".pth"):
        import torch
        import ocrlib.ocrmodels as models

        mname = os.path.basename(path).split("-")[0]
        model = models.make(mname, device="cpu")
        mdict = torch.load(path, map_location=torch.device("cpu"))
        model.load_state_dict(mdict)
        model = flatten_parameters(model)
        return model
    else:
        raise Exception("unknown model type: " + path)


class WordRecognizer:
    def __init__(self, murl, charset=None, device=default_device, maxheight=48.0):
        murl = murl or default_textmodel
        charset = charset or make_ascii_charset()
        self.device = device
        self.charset = charset
        self.model = get_model(murl)
        self.maxheight = maxheight

    def inference(self, images):
        assert all(isinstance(im, np.ndarray) for im in images)
        images = [scale_to_maxheight(im, self.maxheight) for im in images]
        images = [usm_filter(im) for im in images]
        assert all(im.shape[0] <= self.maxheight for im in images)
        input = batch_images(images)  # BDHW
        assert torch.is_tensor(input)
        with OnDevice(self.model, self.device) as model:
            with torch.no_grad():
                assert input.shape[-2] <= self.maxheight
                outputs = model(input)
        outputs = outputs.detach().cpu().softmax(1)
        seqs = [ctc_decode(pred.numpy()) for pred in outputs]
        texts = [self.charset.decode(seq) for seq in seqs]
        return texts


def show_seg(a, ax=None):
    ax = ax or plt.gca()
    ax.imshow(np.where(a == 0, 0, 0.3 + np.abs(np.sin(a))), cmap="gnuplot")


def compute_segmentation(probs, show=True):
    word_markers = probs[:, :, 3] > 0.3
    word_markers = ndi.minimum_filter(ndi.maximum_filter(word_markers, (3, 5)), (3, 5))
    # plt.imshow(word_markers)

    word_labels, n = ndi.label(word_markers)

    _, sources = ndi.distance_transform_edt(1 - word_markers, return_indices=True)
    word_sources = word_labels[sources[0], sources[1]]
    # show_seg(word_sources)

    word_boundaries = (ndi.maximum_filter(word_sources, 5) - ndi.minimum_filter(word_sources, 5) > 0)
    # plt.imshow(word_boundaries)

    # separators = maximum(probs[:,:,1]>0.3, word_boundaries)
    separators = np.maximum(probs[:, :, 1] > 0.5, probs[:, :, 0] > 0.5)
    separators = np.minimum(separators, (probs[:, :, 2] < 0.5))
    separators = np.minimum(separators, (probs[:, :, 3] < 0.5))
    separators = np.maximum(separators, word_boundaries)
    # plt.imshow(separators)
    all_components, n = ndi.label(1 - separators)
    # show_seg(all_components)

    # word_markers = (probs[:,:,3] > 0.5) * (1-separators)
    word_markers = (np.maximum(probs[:, :, 2], probs[:, :, 3]) > 0.5) * (1 - separators)
    word_markers = ndi.minimum_filter(ndi.maximum_filter(word_markers, (1, 3)), (1, 3))
    word_labels, n = ndi.label(word_markers)
    # show_seg(word_labels)

    correspondence = 1000000 * word_labels + all_components
    nwords = np.amax(word_sources) + 1
    ncomponents = np.amax(all_components) + 1

    wordmap = np.zeros(ncomponents, dtype=int)
    for word, comp in [
        (k // 1000000, k % 1000000) for k in np.unique(correspondence.ravel())
    ]:
        if comp == 0:
            continue
        if word == 0:
            continue
        if wordmap[comp] > 0:
            # FIXME do something about ambiguous assignments
            # print(word, comp)
            pass
        wordmap[comp] = word

    result = wordmap[all_components]
    return locals()


def compute_slices(wordmap):
    for s in ndi.find_objects(wordmap):
        if s is not None:
            yield s


def compute_bboxes(wordmap, pad=10, padr=0):
    if isinstance(pad, int):
        pad = (pad, pad, pad, pad)
    if isinstance(padr, (int, float)):
        padr = (padr, padr, padr, padr)
    for ys, xs in compute_slices(wordmap):
        h, w = ys.stop - ys.start, xs.stop - xs.start
        yield dict(
            t=max(ys.start - max(pad[0], int(padr[0] * h)), 0),
            l=max(xs.start - max(pad[1], int(padr[1] * h)), 0),
            b=ys.stop + max(pad[2], int(padr[2] * h)),
            r=xs.stop + max(pad[3], int(padr[3] * h)),
        )

def bbox_all(list_of_bboxes):
    return dict(
        t=min(a["t"] for a in list_of_bboxes),
        l=min(a["l"] for a in list_of_bboxes),
        b=max(a["b"] for a in list_of_bboxes),
        r=max(a["r"] for a in list_of_bboxes),
    )

def bbox_center(a):
    return (a["l"] + a["r"]) / 2, (a["t"] + a["b"]) / 2

def bbox_height(a):
    return a["b"] - a["t"]

def bbox_width(a):
    return a["r"] - a["l"]

def bbox_overlap(a, b):
    t0, l0, b0, r0 = [a[c] for c in "tlbr"]
    t1, l1, b1, r1 = [b[c] for c in "tlbr"]
    return max(0, min(b0, b1) - max(t0, t1)) * max(0, min(r0, r1) - max(l0, l1))

def bbox_area(a):
    return (a["b"] - a["t"]) * (a["r"] - a["l"])

def bbox_overlap_frac(a, b):
    area = min(bbox_area(a), bbox_area(b))
    return bbox_overlap(a, b) * 1.0 / area

def bbox_same_line(a, b):
    # b is on the same line as a
    delta = bbox_height(a) * 0.3
    xc, yc = bbox_center(b)
    assert a["t"] <= a["b"]
    return b["t"] > a["t"] - delta and b["b"] < a["b"] + delta

def bbox_merge(a, b):
    t0, l0, b0, r0 = [a[c] for c in "tlbr"]
    t1, l1, b1, r1 = [b[c] for c in "tlbr"]
    return dict(
        t=min(t0, t1),
        l=min(l0, l1),
        b=max(b0, b1),
        r=max(r0, r1),
    )

def merge_overlapping(bboxes, threshold=3, rthreshold=0.1):
    bboxes = list(bboxes)
    for i in range(len(bboxes)):
        for j in range(len(bboxes)):
            if i == j or bboxes[i] is None or bboxes[j] is None:
                continue
            height = bbox_height(bboxes[i])
            if bbox_width(bboxes[j]) < height:
                if bbox_same_line(bboxes[j], bboxes[i]) and bbox_overlap_frac(bboxes[i], bboxes[j]) > 0.5:
                    bboxes[i] = bbox_merge(bboxes[i], bboxes[j])
                    bboxes[j] = None
    return [b for b in bboxes if b is not None]

# bboxes = list(compute_bboxes(probs, pad=10))

def reading_order(lines: List[dict], highlight=None, binary=None, debug=0):
    """Given the list of lines (a list of dicts with tlbr bounding boxes),
    computes the partial reading order.  The output is a binary 2D array
    such that order[i,j] is true if line i comes before line j
    in reading order."""
    order = np.zeros((len(lines), len(lines)), "B")

    def x_overlaps(u, v):
        return u["l"] < v["r"] and u["r"] > v["l"]

    def above(u, v):
        return u["b"] < v["t"]

    def left_of(u, v):
        return u["r"] < v["l"]

    def separates(w, u, v):
        if w["b"] < min(u["t"], v["t"]):
            return 0
        if w["t"] > max(u["b"], v["b"]):
            return 0
        if w["l"] < u["r"] and w["r"] > v["l"]:
            return 1
        return 0

    def center(bbox):
        return (bbox["l"] + bbox["r"]) / 2, (bbox["t"] + bbox["b"]) / 2

    if highlight is not None:
        plt.clf()
        plt.title("highlight")
        plt.imshow(binary)
        plt.ginput(1, debug)
    for i, u in enumerate(lines):
        for j, v in enumerate(lines):
            if x_overlaps(u, v):
                if above(u, v):
                    order[i, j] = 1
            else:
                if [w for w in lines if separates(w, u, v)] == []:
                    if left_of(u, v):
                        order[i, j] = 1
            if j == highlight and order[i, j]:
                print((i, j), end=" ")
                y0, x0 = center(lines[i])
                y1, x1 = center(lines[j])
                plt.plot([x0, x1 + 200], [y0, y1])
    if highlight is not None:
        print()
        plt.ginput(1, debug)
    return order


def find(condition):
    "Return the indices where ravel(condition) is true"
    (res,) = np.nonzero(np.ravel(condition))
    return res


def topsort(order):
    """Given a binary array defining a partial order (o[i,j]==True means i<j),
    compute a topological sort.  This is a quick and dirty implementation
    that works for up to a few thousand elements."""
    n = len(order)
    visited = np.zeros(n)
    L = []

    def visit(k):
        if visited[k]:
            return
        visited[k] = 1
        for l in find(order[:, k]):
            visit(l)
        L.append(k)

    for k in range(n):
        visit(k)
    return L  # [::-1]



import matplotlib.patches as patches


def draw_bboxes(boxes, ax=None):
    ax = ax or plt.gca()
    for box in boxes:
        t, l, b, r = [box[c] for c in "tlbr"]
        ax.add_patch(
            patches.Rectangle(
                (l, t), r - l, b - t, linewidth=1, edgecolor="r", facecolor="none"
            )
        )


def show_extracts(image, bboxes, nrows=4, ncols=3):
    # from IPython.display import display
    fig, axs = plt.subplots(nrows, ncols)
    axs = axs.ravel()

    for i in range(nrows * ncols):
        if i >= len(bboxes):
            break
        t, l, b, r = [bboxes[i][c] for c in "tlbr"]
        axs[i].imshow(image[t:b, l:r])
        # axs[i].set_xticks([])
        # axs[i].set_yticks([])
        # axs[i].axis()

    # display(fig)


def download_file(url, filename, overwrite=False):
    if os.path.exists(filename) and not overwrite:
        return filename
    assert 0 == os.system(f"curl -L -o '{filename}' '{url}'")
    return
    print(f"Downloading {url} to {filename}")
    with requests.get(url, stream=True) as r:
        with open(filename, "wb") as f:
            shutil.copyfileobj(r.raw, f)
    return filename



def autoinvert(image):
    if image.shape[0] < 2 or image.shape[1] < 2:
        return image
    middle = (np.amax(image) + np.amin(image)) / 2
    if np.mean(image) > middle:
        return 1 - image
    else:
        return image
    

def compute_linemap(probs):
    marker = probs[:, :, 6]> 0.3
    marker = ndi.minimum_filter(ndi.maximum_filter(marker, (5, 10)), (5, 10))
    marker1 = remove_small_connected_components(marker, 100)
    labels, n = ndi.label(marker1)
    labels1 = spread_labels(labels, 100)
    return labels1

def assign_bboxes_to_lines(bboxes, linemap):
    for bbox in bboxes:
        xc, yc = bbox_center(bbox)
        bbox["lineno"] = linemap[int(yc), int(xc)]
    return max([bbox["lineno"] for bbox in bboxes] + [0]) + 1

def bbox_patch(box, text=None, ax=None, linewidth=1, rcolor="r", alpha=1.0, facecolor="none", fontsize=12, color="r", offset=(0, 0)):
    t, l, b, r = [box[c] for c in "tlbr"]
    ax.text(l + offset[0], t + offset[1], text, fontsize=fontsize, color=color)
    # draw a rectangle around the word
    ax.add_patch(
        patches.Rectangle(
            (l, t),
            r - l,
            b - t,
            linewidth=linewidth,
            edgecolor=rcolor,
            facecolor=facecolor,
            alpha=alpha,
        )
    )


class PageRecognizer:
    def __init__(self, segmodel=None, textmodel=None, device=default_device):
        self.device = device
        self.segmenter = PageSegmenter(segmodel, device=device)
        self.textmodel = WordRecognizer(textmodel, device=device)
        self.words_per_batch = 64

    def to(self, device):
        self.device = device

    def valid_binary_image(self, binarized, verbose=False):
        h, w = binarized.shape
        if h < 10 and w < 10:
            return False
        if h > 200:
            return False
        if h < 5 or w < 5:
            return False
        if not (np.amin(binarized) < 0.1 and np.amax(binarized) > 0.9):
            return False
        if False:  # FIXME
            if np.mean(binarized) > 0.5:
                binarized = 1 - binarized
            if np.sum(binarized > 0.9) < 0.05 * h * w:
                if verbose:
                    print("insufficient white")
                return False
        return True

    def recognize(self, image, keep_images=False, preproc="none"):
        if image.ndim == 3:
            image = np.mean(image[:, :, :3], axis=2)
        self.image = image
        self.bin = nlbin.nlbin(image, deskew=False)
        if preproc == "none":
            srcimg = self.image
        elif preproc == "binarize":
            srcimg = self.bin
        elif preproc == "threshold":
            srcimg = (self.bin > 0.5).astype(np.float32)
        else:
            raise ValueError("preproc must be one of none, binarize, threshold")
        self.srcimg = srcimg
        self.seg_full = self.segmenter.inference(srcimg)
        self.seg_probs = self.seg_full[:, :, :4]  # word segmentation only
        self.segmentation = compute_segmentation(self.seg_probs)
        self.wordmap = self.segmentation["result"]
        self.bboxes = list(compute_bboxes(self.wordmap))
        self.bboxes = merge_overlapping(self.bboxes)
        for i in range(len(self.bboxes)):
            t, l, b, r = [self.bboxes[i][c] for c in "tlbr"]
            box = self.bboxes[i]
            box["image"] = autoinvert(srcimg[t:b, l:r])
            box["binarized"] = autoinvert(self.bin[t:b, l:r])
        self.bboxes = bboxes = [
            b for b in self.bboxes if self.valid_binary_image(b["binarized"])
        ]
        for i in range(0, len(self.bboxes), self.words_per_batch):
            bboxes = self.bboxes[i : i + self.words_per_batch]
            images = [b["image"] for b in self.bboxes[i : i + self.words_per_batch]]
            pred = self.textmodel.inference(images)
            assert len(pred) == len(bboxes)
            for i in range(len(bboxes)):
                bboxes[i]["text"] = pred[i]
        if self.seg_full.shape[2] == 7:
            self.linemap = compute_linemap(self.seg_full)
            nlines = assign_bboxes_to_lines(self.bboxes, self.linemap)
            raw_lines = [[] for i in range(nlines)]
            for b in self.bboxes:
                raw_lines[b["lineno"]].append(b)
            for i in range(nlines):
                raw_lines[i] = sorted(raw_lines[i], key=lambda b: b["l"])
            self.lines = [dict(words=raw_lines[i], **bbox_all(raw_lines[i])) for i in range(nlines) if len(raw_lines[i]) > 0]
            self.partial_reading_order = reading_order(self.lines)          
            self.line_index = topsort(self.partial_reading_order)
            new_lines = [[] for i in range(max(self.line_index) + 1)]
            for i in range(len(self.line_index)):
                new_lines[i] = self.lines[self.line_index[i]]
            self.lines = new_lines
        else:
            self.lines = [dict(words=self.bboxes, **bbox_all(self.bboxes))] if len(self.bboxes) > 0 else []
        if not keep_images:
            for b in self.bboxes:
                del b["image"]
                del b["binarized"]
        return self.bboxes

    def draw_overlaid(
        self, fontsize=6, offset=(5, 10), color="red", rcolor="red", lcolor="green", alpha=0.25, ax=None
    ):
        if ax is None:
            fig, ax = plt.subplots(1, 1, figsize=(20, 20))
        ax.imshow(self.srcimg)
        for i in range(len(self.bboxes)):
            box = self.bboxes[i]
            text = box["text"]
            bbox_patch(box, text=text, ax=ax, fontsize=fontsize, color=color, offset=offset)
        for l in self.lines:
            bbox_patch(l, ax=ax, rcolor=lcolor)


    def draw_words(self, nrows=6, ncols=4, ax=None):
        bboxes = list(self.bboxes)
        random.shuffle(bboxes)
        n = min(nrows * ncols, len(bboxes))
        for i in range(n):
            box = bboxes[i]
            image = box["image"]
            text = box["text"]
            plt.subplot(nrows, ncols, i + 1)
            plt.imshow(1 - image.numpy()[0])
            plt.xticks([])
            plt.yticks([])
            h, w = image.shape[-2:]
            plt.gca().text(5, 12, text, fontsize=9, color="red")
            # title(pred[i], fontsize=6)
