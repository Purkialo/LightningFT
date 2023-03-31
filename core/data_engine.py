import os
import json

import lmdb
import torch
import numpy as np
import torchvision
from tqdm import tqdm

from utils.utils import pretty_dict

class DataEngine:
    def __init__(self, path_dict):
        self.path_dict = path_dict
        self.path_dict['dataset_path'] = os.path.join(path_dict['output_path'], 'lmdb')
        self.path_dict['lmks_path'] = os.path.join(path_dict['output_path'], 'landmarks.pth')
        self.path_dict['emoca_path'] = os.path.join(path_dict['output_path'], 'emoca_v2.pth')
        self.path_dict['camera_path'] = os.path.join(path_dict['output_path'], 'camera_params.pth')
        self.path_dict['lightning_path'] = os.path.join(path_dict['output_path'], 'lightning.json')
        self.path_dict['anal_by_synth_path'] = os.path.join(path_dict['output_path'], 'anal_by_synth.json')
        self.path_dict['smoothed_path'] = os.path.join(path_dict['output_path'], 'smoothed_results.json')
        self.path_dict['visul_path'] = os.path.join(path_dict['output_path'], 'track.mp4')
        self.path_dict['calib_path'] = os.path.join(path_dict['output_path'], 'calibration.jpg')

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

    def get_frames(self, frame_names, channel=3, keys=[]):
        frames, emoca_params, gt_landmarks = [], [], []
        for f in frame_names:
            frame = self.get_frame(f, channel=channel)
            if 'annotation' in keys:
                emo = self.get_emoca_params(f)
                lmk = self.get_landmarks(f)
                if emo is not None:
                    frames.append(frame)
                    emoca_params.append(emo)
                    gt_landmarks.append(lmk)
            else:
                frames.append(frame)
        frames = torch.utils.data.default_collate(frames)
        if 'annotation' in keys:
            emoca_params = torch.utils.data.default_collate(emoca_params)
            gt_landmarks = torch.utils.data.default_collate(gt_landmarks)
        return {'frames': frames, 'emoca': emoca_params, 'landmarks': gt_landmarks}

    def get_landmarks(self, frame_name):
        if not hasattr(self, 'landmarks'):
            self.landmarks = torch.load(self.path_dict['lmks_path'], map_location='cpu')
        return self.landmarks[frame_name]

    def get_emoca_params(self, frame_name):
        if not hasattr(self, 'emoca_params'):
            self.emoca_params = torch.load(self.path_dict['emoca_path'], map_location='cpu')
        return self.emoca_params[frame_name]

    def get_camera_params(self, ):
        if not hasattr(self, 'camera_params'):
            self.camera_params = torch.load(self.path_dict['camera_path'], map_location='cpu')
        return self.camera_params

    def check_path(self, path_key):
        if os.path.exists(self.path_dict[path_key]):
            print('Found {}.'.format(self.path_dict[path_key]))
            return True
        else:
            return False

    def save(self, data, path_key):
        if '.pth' in self.path_dict[path_key]:
            torch.save(data, self.path_dict[path_key])
        elif '.json' in self.path_dict[path_key]:
            with open(self.path_dict[path_key], "w") as f:
                json.dump(data, f)

    def build_data_lmdb(self, ):
        if not os.path.exists(self.path_dict['dataset_path']):
            print('Decoding video.....')
            frames, _, meta_data = torchvision.io.read_video(
                self.path_dict['video_path'], pts_unit='sec', output_format='TCHW'
            )
            frames = torchvision.transforms.functional.resize(frames, size=512, antialias=True)
            frames = torchvision.transforms.functional.center_crop(frames, output_size=512).float()
            print('Dumpling video to buffer lmdb.....')
            os.makedirs(self.path_dict['dataset_path'])
            env = lmdb.open(self.path_dict['dataset_path'], map_size=1099511627776) # Maximum 1T
            txn = env.begin(write=True)
            counter = 0
            for f_idx, frame in enumerate(tqdm(frames, ncols=80, colour='#95bb72')):
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
            print('Load data, length:{}.'.format(len(all_keys)))
            frames = [key.decode() for key in all_keys]
            frames.sort(key=lambda x:int(x[2:-4]))
            self._frames = frames
        return self._frames[:10]