import os
import json

import lmdb
import torch
import numpy as np
import torchvision
from tqdm.rich import tqdm

from model.SGHM import HumanMatting
from utils.utils import pretty_dict

SGHM_CKPT_PATH = './assets/SGHM/SGHM-ResNet50.pth'

class DataEngine:
    def __init__(self, path_dict, device='cpu'):
        self.device = device
        self.path_dict = path_dict
        self.path_dict['dataset_path'] = os.path.join(path_dict['output_path'], 'lmdb')
        # self.path_dict['lmks_path'] = os.path.join(path_dict['output_path'], 'landmarks.pth')
        self.path_dict['emoca_path'] = os.path.join(path_dict['output_path'], 'emoca.pth')
        self.path_dict['camera_path'] = os.path.join(path_dict['output_path'], 'camera_params.pth')
        self.path_dict['lightning_path'] = os.path.join(path_dict['output_path'], 'lightning.pth')
        self.path_dict['texture_path'] = os.path.join(path_dict['output_path'], 'texture.pth')
        self.path_dict['synthesis_path'] = os.path.join(path_dict['output_path'], 'synthesis.pth')
        self.path_dict['smoothed_path'] = os.path.join(path_dict['output_path'], 'smoothed.pth')
        self.path_dict['visul_path'] = os.path.join(path_dict['output_path'], 'track.mp4')
        self.path_dict['visul_data_path'] = os.path.join(path_dict['output_path'], 'data_vis.mp4')
        self.path_dict['visul_calib_path'] = os.path.join(path_dict['output_path'], 'calibration.jpg')
        self.path_dict['visul_texture_path'] = os.path.join(path_dict['output_path'], 'texture.jpg')

    def __str__(self, ):
        return pretty_dict(self.path_dict)

    def get_frame(self, frame_name, channel=3):
        if not hasattr(self, '_dataset_lmdb_env'):
            self._dataset_lmdb_env = lmdb.open(
                self.path_dict['dataset_path'], readonly=True, lock=False, readahead=False, meminit=True
            ) 
            self._dataset_lmdb_txn = self._dataset_lmdb_env.begin(write=False)
        # load image as [channel(RGB), image_height, image_width]
        _mode = torchvision.io.ImageReadMode.RGB if channel == 3 else torchvision.io.ImageReadMode.GRAY
        image_buf = self._dataset_lmdb_txn.get(frame_name.encode())
        image_buf = torch.tensor(np.frombuffer(image_buf, dtype=np.uint8))
        image = torchvision.io.decode_image(image_buf, mode=_mode)
        # image = torchvision.io.read_image(frame_name, mode=_mode)
        assert image is not None, frame_name
        return image

    def get_frames(self, frame_names, channel=3, keys=[], *, device='cpu'):
        results = {'frame_names': [], 'frames': []}
        for k in keys:
            results[k] = []
        for f in frame_names:
            for k in keys:
                results[k].append(self.get_data(k+'_path', query_name=f))
            results['frames'].append(self.get_frame(f, channel=channel))
            results['frame_names'].append(f)
        results['frames'] = torch.utils.data.default_collate(results['frames'])
        for k in keys:
            results[k] = torch.utils.data.default_collate(results[k])
        results = move_to(results, dtype=torch.float32, device=device)
        return results

    def get_data(self, path_key, device='cpu', *, query_name=None):
        if not hasattr(self, path_key.replace('path', 'data')):
            setattr(
                self, path_key.replace('path', 'data'), 
                torch.load(self.path_dict[path_key], map_location='cpu')
            )
        data = getattr(self, path_key.replace('path', 'data'))
        if query_name is None:
            return move_to(data, dtype=torch.float32, device=device)
        else:
            return move_to(data[query_name], dtype=torch.float32, device=device)

    def check_path(self, path_key):
        if os.path.exists(self.path_dict[path_key]):
            print('Found {}.'.format(self.path_dict[path_key]))
            return True
        else:
            return False

    def save(self, data, path_key, **kwargs):
        if '.pth' in self.path_dict[path_key]:
            torch.save(data, self.path_dict[path_key])
        elif '.json' in self.path_dict[path_key]:
            with open(self.path_dict[path_key], "w") as f:
                json.dump(data, f)
        elif '.mp4' in self.path_dict[path_key]:
            print('Writing video.....')
            torchvision.io.write_video(self.path_dict[path_key], data, fps=kwargs['fps'])
            print('Done.')
        elif '.jpg' in self.path_dict[path_key]:
            torchvision.utils.save_image(data, self.path_dict[path_key], nrow=4)

    def build_data_lmdb(self, matting_thresh):
        if not os.path.exists(self.path_dict['dataset_path']):
            self.matting_engine = RobustMattingEngine(device=self.device)
            print('Decoding video.....')
            video = torchvision.io.VideoReader(self.path_dict['video_path'], 'video')
            meta_data = video.get_metadata()
            approx_len = int(meta_data['video']['duration'][0] * meta_data['video']['fps'][0]) + 1
            print('Dumpling video to buffer lmdb.....')
            os.makedirs(self.path_dict['dataset_path'])
            env = lmdb.open(self.path_dict['dataset_path'], map_size=1099511627776) # Maximum 1T
            txn = env.begin(write=True)
            counter = 0
            visulization, writed = [], False
            for f_idx, frame in enumerate(tqdm(video, ncols=80, colour='#95bb72', total=approx_len)):
                frame = frame['data']
                frame = self.matting_engine.matting(frame, thresh=matting_thresh)
                frame = torchvision.transforms.functional.resize(frame, size=512, antialias=True)
                frame = torchvision.transforms.functional.center_crop(frame, output_size=512).float()
                if f_idx % 3 == 0 and len(visulization) < 100:
                    visulization.append(frame.cpu())
                elif len(visulization) >= 100 and not writed:
                    visulization = torch.stack(visulization, dim=0).permute(0, 2, 3, 1)
                    torchvision.io.write_video(
                        self.path_dict['visul_data_path'], visulization, fps=10
                    )
                    writed = True
                img_name = 'f_{:07d}.jpg'.format(f_idx)
                img_encoded = torchvision.io.encode_jpeg(frame.to(torch.uint8))
                img_encoded = b''.join(map(lambda x:int.to_bytes(x,1,'little'), img_encoded.numpy().tolist()))
                buf = txn.get(img_name.encode())
                if buf is not None:
                    print('Exsist!', img_name)
                    continue
                else:
                    txn.put(img_name.encode(), img_encoded)
                    counter += 1
                    if counter % 1000 == 0:
                        txn.commit()
                        txn = env.begin(write=True)
            txn.commit()
            env.close()
            print('Data has been built.')
        else:
            print('Load buffered data.')

    def frames(self, ):
        if not hasattr(self, '_dataset_lmdb_env'):
            self._dataset_lmdb_env = lmdb.open(
                self.path_dict['dataset_path'], readonly=True, lock=False, readahead=False, meminit=True
            ) 
            self._dataset_lmdb_txn = self._dataset_lmdb_env.begin(write=False)
        if not hasattr(self, '_frames'):
            frames = []
            all_keys = list(self._dataset_lmdb_txn.cursor().iternext(values=False))
            frames = [key.decode() for key in all_keys]
            print('Load data, length:{}.'.format(len(frames)))
            frames.sort(key=lambda x:int(x[2:-4]))
            self._frames = frames
        return self._frames


class RobustMattingEngine:
    def __init__(self, device='cuda'):
        self.device = device
        mat_model = HumanMatting(backbone='resnet50').eval()
        ckpt = torch.load(SGHM_CKPT_PATH, map_location='cpu')
        new_ckpt = {}
        for key in ckpt.keys():
            new_ckpt[key[7:]] = ckpt[key] 
        mat_model.load_state_dict(new_ckpt)
        self.mat_model = mat_model.to(device)
        print('Matting Model Build done.')
        self.rec_frames = [None] * 4

    @torch.no_grad()
    def matting(self, frame, thresh=0.3, background=[255, 255, 255]):
        infer_size = 1280
        h, w = frame.shape[1], frame.shape[2]
        if w >= h:
            rh = infer_size
            rw = int(w / h * infer_size)
        else:
            rw = infer_size
            rh = int(h / w * infer_size)
        rh = rh - rh % 64
        rw = rw - rw % 64    
        resized_frame = torchvision.transforms.functional.resize(frame, size=(rh, rw), antialias=True)
        resized_frame = resized_frame[None]/255.0
        matting_results = self.mat_model(resized_frame.to(self.device))
        alpha = matting_results['alpha_os8']
        alpha = torchvision.transforms.functional.resize(alpha, size=(h, w), antialias=True)[0, 0]
        frame[:, alpha < thresh] = torch.tensor(background).to(torch.uint8)[:, None]
        return frame


def move_to(obj, dtype, device):
    if torch.is_tensor(obj):
        return obj.to(device=device, dtype=dtype)
    elif isinstance(obj, dict):
        res = {}
        for k, v in obj.items():
            res[k] = move_to(v, dtype, device)
        return res
    elif isinstance(obj, list):
        res = []
        for v in obj:
            res.append(move_to(v, dtype, device))
        return res
    elif isinstance(obj, str) or isinstance(obj, float) or isinstance(obj, int):
        return obj
    else:
        print(obj, type(obj))
        raise TypeError("Invalid type for move_to")
