"""Functions for video handling."""

import logging
import os
import shutil
import subprocess
from typing import Optional

import cv2
import numpy as np
import pandas as pd
from lightning.app import LightningWork
from tqdm import tqdm

_logger = logging.getLogger('APP.BACKEND.VIDEO')


def check_codec_format(input_file: str) -> bool:
    """Run FFprobe command to get video codec and pixel format."""

    ffmpeg_cmd = f'ffmpeg -i {input_file}'
    output_str = subprocess.run(ffmpeg_cmd, shell=True, capture_output=True, text=True)
    # stderr because the ffmpeg command has no output file, but the stderr still has codec info.
    output_str = output_str.stderr

    # search for correct codec (h264) and pixel format (yuv420p)
    if output_str.find('h264') != -1 and output_str.find('yuv420p') != -1:
        # print('Video uses H.264 codec')
        is_codec = True
    else:
        # print('Video does not use H.264 codec')
        is_codec = False
    return is_codec


def reencode_video(input_file: str, output_file: str) -> None:
    """reencodes video into H.264 coded format using ffmpeg from a subprocess.

    Args:
        input_file: abspath to existing video
        output_file: abspath to to new video

    """
    # check input file exists
    assert os.path.isfile(input_file), "input video does not exist."
    # check directory for saving outputs exists
    os.makedirs(os.path.dirname(output_file), exist_ok=True)
    ffmpeg_cmd = (
        f'ffmpeg -i {input_file} '
        f'-vf "pad=ceil(iw/2)*2:ceil(ih/2)*2" -c:v libx264 -pix_fmt yuv420p '
        f'-c:a copy -y {output_file}'
    )
    subprocess.run(ffmpeg_cmd, shell=True)


def copy_and_reformat_video(video_file: str, dst_dir: str, remove_old: bool = True) -> str:
    """Copy a single video, reformatting if necessary, and delete the original."""

    src = video_file

    # make sure copied vid has mp4 extension
    dst = os.path.join(dst_dir, os.path.basename(video_file).replace(".avi", ".mp4"))

    # check 0: do we even need to reformat?
    if os.path.isfile(dst):
        return dst

    # check 1: does file exist?
    if not os.path.exists(src):
        _logger.info(f"{src} does not exist! skipping")
        return None

    # check 2: is file in the correct format for DALI?
    video_file_correct_codec = check_codec_format(src)

    # reencode/rename
    if not video_file_correct_codec:
        _logger.info(f"re-encoding {src} to be compatable with Lightning Pose video reader")
        reencode_video(src, dst)
        # remove old video
        if remove_old:
            os.remove(src)
    else:
        # make dir to write into
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        # rename
        if remove_old:
            os.rename(src, dst)
        else:
            shutil.copyfile(src, dst)

    return dst


def copy_and_reformat_video_directory(src_dir: str, dst_dir: str) -> None:
    """Copy a directory of videos, reencoding to be DALI compatible if necessary."""

    os.makedirs(dst_dir, exist_ok=True)
    video_dir_contents = os.listdir(src_dir)
    for file_or_dir in video_dir_contents:
        src = os.path.join(src_dir, file_or_dir)
        dst = os.path.join(dst_dir, file_or_dir)
        if os.path.isdir(src):
            # don't copy subdirectories in video directory
            continue
        else:
            if src.endswith(".mp4") or src.endswith(".avi"):
                video_file_correct_codec = check_codec_format(src)
                if not video_file_correct_codec:
                    _logger.info(
                        f"re-encoding {src} to be compatable with Lightning Pose video reader")
                    reencode_video(src, dst.replace(".avi", ".mp4"))
                else:
                    # copy already-formatted video
                    shutil.copyfile(src, dst)
            else:
                # copy non-video files
                shutil.copyfile(src, dst)


def get_frames_from_idxs(cap: cv2.VideoCapture, idxs: np.ndarray) -> np.ndarray:
    """Helper function to load video segments.

    Parameters
    ----------
    cap : cv2.VideoCapture object
    idxs : array-like
        frame indices into video

    Returns
    -------
    np.ndarray
        returned frames of shape (n_frames, n_channels, ypix, xpix)

    """
    is_contiguous = np.sum(np.diff(idxs)) == (len(idxs) - 1)
    n_frames = len(idxs)
    for fr, i in enumerate(idxs):
        if fr == 0 or not is_contiguous:
            cap.set(1, i)
        ret, frame = cap.read()
        if ret:
            frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            if fr == 0:
                height, width, _ = frame_rgb.shape
                frames = np.zeros((n_frames, 3, height, width), dtype="uint8")
            frames[fr] = frame_rgb.transpose(2, 0, 1)
        else:
            _logger.debug(
                "warning! reached end of video; returning blank frames for remainder of "
                + "requested indices"
            )
            break
    return frames


def make_video_snippet(
    video_file: str,
    preds_file: Optional[str] = None,
    clip_length: int = 30,
    likelihood_thresh: float = 0.9
) -> tuple:
    """Create a snippet from a larger video that contains the most movement.

    Parameters
    ----------
    video_file: absolute path to original video file
    preds_file: csv file containing pose estimates; if not None, "movement" is measured from these
        pose estimates; if None, "movement" is measured using the pixel values
    clip_length: length of the snippet in seconds
    likelihood_thresh: only measure movement using the preds_file for keypoints with a likelihood
        above this threshold (0-1)

    Returns
    -------
    - absolute path to video snippet
    - snippet start: frame index
    - snippet start: seconds

    """

    # how large is the clip window?
    video = cv2.VideoCapture(video_file)
    fps = video.get(cv2.CAP_PROP_FPS)
    win_len = int(fps * clip_length)
    n_frames = int(video.get(cv2.CAP_PROP_FRAME_COUNT))

    # manage paths
    if preds_file is None:
        # save videos with video file
        save_dir = os.path.dirname(video_file)
    else:
        # save videos with csv file
        save_dir = os.path.dirname(preds_file)

    src = video_file
    new_video_name = os.path.basename(video_file).replace(
        ".mp4", ".short.mp4"
    ).replace(
        ".avi", ".short.mp4"
    )
    dst = os.path.join(save_dir, new_video_name)

    # make a `clip_length` second video clip that contains the highest keypoint motion energy
    if win_len >= n_frames:
        # short video, no need to shorten further. just copy existing video
        clip_start_idx = 0
        clip_start_sec = 0.0
        if not os.path.exists(dst):
            shutil.copyfile(src, dst)
    else:
        # compute motion energy
        if preds_file is None:
            # motion energy averaged over pixels
            me = compute_video_motion_energy(video_file=video_file)
        else:
            # load pose predictions
            df = pd.read_csv(preds_file, header=[0, 1, 2], index_col=0)
            # motion energy averaged over predicted keypoints
            me = compute_motion_energy_from_predection_df(df, likelihood_thresh)
        # find window
        df_me = pd.DataFrame({"me": me})
        df_me_win = df_me.rolling(window=win_len, center=False).mean()
        # rolling places results in right edge of window, need to subtract this
        clip_start_idx = df_me_win.me.argmax() - win_len
        # convert to seconds
        clip_start_sec = int(clip_start_idx / fps)
        # if all predictions are bad, make sure we still create a valid snippet video
        if np.isnan(clip_start_sec) or clip_start_sec < 0:
            clip_start_sec = 0

        # make clip
        if not os.path.exists(dst):
            ffmpeg_cmd = f"ffmpeg -ss {clip_start_sec} -i {src} -t {clip_length} {dst}"
            subprocess.run(ffmpeg_cmd, shell=True)

    return dst, int(clip_start_idx), float(clip_start_sec)


def compute_video_motion_energy(
    video_file: str,
    resize_dims: int = 32,
) -> np.ndarray:
    # read all frames, reshape, chop off unwanted portions of beginning/end
    frames = read_nth_frames(
        video_file=video_file,
        n=1,
        resize_dims=resize_dims,
    )
    frame_count = frames.shape[0]
    batches = np.reshape(frames, (frame_count, -1))

    # take temporal diffs
    me = np.concatenate([
        np.zeros((1, batches.shape[1])),
        np.diff(batches, axis=0)
    ])
    # take absolute values and sum over all pixels to get motion energy
    me = np.sum(np.abs(me), axis=1)

    return me


def compute_motion_energy_from_predection_df(
    df: pd.DataFrame,
    likelihood_thresh: float,
) -> np.ndarray:

    # Convert predictions to numpy array and reshape
    kps_and_conf = df.to_numpy().reshape(df.shape[0], -1, 3)
    kps = kps_and_conf[:, :, :2]
    conf = kps_and_conf[:, :, -1]
    # Duplicate likelihood scores for x and y coordinates
    conf2 = np.concatenate([conf[:, :, None], conf[:, :, None]], axis=2)

    # Apply likelihood threshold
    kps[conf2 < likelihood_thresh] = np.nan

    # Compute motion energy
    me = np.nanmean(np.linalg.norm(kps[1:] - kps[:-1], axis=2), axis=-1)
    me = np.concatenate([[0], me])
    return me


def read_nth_frames(
    video_file: str,
    n: int = 1,
    resize_dims: int = 64,
    progress_delta: float = 0.5,  # for online progress updates
    work: Optional[LightningWork] = None,  # for online progress updates
) -> np.ndarray:

    # Open the video file
    cap = cv2.VideoCapture(video_file)

    if not cap.isOpened():
        _logger.error(f"Error opening video file {video_file}")

    frames = []
    frame_counter = 0
    frame_total = cap.get(cv2.CAP_PROP_FRAME_COUNT)
    with tqdm(total=int(frame_total)) as pbar:
        while cap.isOpened():
            # Read the next frame
            ret, frame = cap.read()
            if ret:
                # If the frame was successfully read, then process it
                if frame_counter % n == 0:
                    frame_resize = cv2.resize(frame, (resize_dims, resize_dims))
                    frame_gray = cv2.cvtColor(frame_resize, cv2.COLOR_BGR2RGB)
                    frames.append(frame_gray.astype(np.float16))
                frame_counter += 1
                progress = frame_counter / frame_total * 100.0
                # periodically update progress of worker if available
                if work is not None:
                    if round(progress, 4) - work.progress >= progress_delta:
                        if progress > 100:
                            work.progress = 100.0
                        else:
                            work.progress = round(progress, 4)
                pbar.update(1)
            else:
                # If we couldn't read a frame, we've probably reached the end
                break

    # When everything is done, release the video capture object
    cap.release()

    return np.array(frames)
