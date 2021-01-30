from abc import ABC, abstractmethod
from random import randint
import pickle
from pathlib import Path
import os
import zlib
import numpy as np

from agents.util import collate


def check_samples_crc(original_po, original_a, original_o, original_r, original_d, rebuilt_po, rebuilt_a, rebuilt_o, rebuilt_r, rebuilt_d):
    assert str(original_po) == str(rebuilt_po), f"previous observations don't match:\noriginal:\n{original_po}\n!= rebuilt:\n{rebuilt_po}"
    assert str(original_a) == str(rebuilt_a), f"actions don't match:\noriginal:\n{original_a}\n!= rebuilt:\n{rebuilt_a}"
    assert str(original_o) == str(rebuilt_o), f"observations don't match:\noriginal:\n{original_o}\n!= rebuilt:\n{rebuilt_o}"
    assert str(original_r) == str(rebuilt_r), f"rewards don't match:\noriginal:\n{original_r}\n!= rebuilt:\n{rebuilt_r}"
    assert str(original_d) == str(rebuilt_d), f"dones don't match:\noriginal:\n{original_d}\n!= rebuilt:\n{rebuilt_d}"
    original_crc = zlib.crc32(str.encode(str((original_a, original_o, original_r, original_d))))
    crc = zlib.crc32(str.encode(str((rebuilt_a, rebuilt_o, rebuilt_r, rebuilt_d))))
    assert crc == original_crc, f"CRC failed: new crc:{crc} != old crc:{original_crc}.\nEither the custom pipeline is corrupted, or crc_debug is False in the rollout worker.\noriginal sample:\n{(original_a, original_o, original_r, original_d)}\n!= rebuilt sample:\n{(rebuilt_a, rebuilt_o, rebuilt_r, rebuilt_d)}"
    print("DEBUG: CRC check passed.")


def check_samples_crc_traj(original_o, original_r, original_d, rebuilt_o, rebuilt_r, rebuilt_d):
    assert str(original_o) == str(rebuilt_o), f"observations don't match:\noriginal:\n{original_o}\n!= rebuilt:\n{rebuilt_o}"
    assert str(original_r) == str(rebuilt_r), f"rewards don't match:\noriginal:\n{original_r}\n!= rebuilt:\n{rebuilt_r}"
    assert str(original_d) == str(rebuilt_d), f"dones don't match:\noriginal:\n{original_d}\n!= rebuilt:\n{rebuilt_d}"
    original_crc = zlib.crc32(str.encode(str((original_o, original_r, original_d))))
    crc = zlib.crc32(str.encode(str((rebuilt_o, rebuilt_r, rebuilt_d))))
    assert crc == original_crc, f"CRC failed: new crc:{crc} != old crc:{original_crc}.\nEither the custom pipeline is corrupted, or crc_debug is False in the rollout worker.\noriginal sample:\n{(original_o, original_r, original_d)}\n!= rebuilt sample:\n{(rebuilt_o, rebuilt_r, rebuilt_d)}"
    print("DEBUG: CRC check passed.")


class MemoryDataloading(ABC):
    def __init__(self,
                 memory_size,
                 batchsize,
                 device,
                 path_loc,
                 remove_size=100,
                 obs_preprocessor: callable = None,
                 sample_preprocessor: callable = None,
                 crc_debug=False):
        self.device = device
        self.batchsize = batchsize
        self.memory_size = memory_size
        self.remove_size = remove_size
        self.obs_preprocessor = obs_preprocessor
        self.sample_preprocessor = sample_preprocessor
        self.crc_debug = crc_debug

        # These stats are here because they reach the trainer along with the buffer:
        self.stat_test_return = 0.0
        self.stat_train_return = 0.0
        self.stat_test_steps = 0
        self.stat_train_steps = 0

        # init memory
        self.path = Path(path_loc)
        if os.path.isfile(self.path / 'data.pkl'):
            with open(self.path / 'data.pkl', 'rb') as f:
                self.data = list(pickle.load(f))
                print(f"DEBUG: len data:{len(self.data)}")
                print(f"DEBUG: len data[0]:{len(self.data[0])}")
        else:
            print("INFO: no data found, initializing empty replay memory")
            self.data = []

        if len(self) > self.memory_size:
            # TODO: crop to memory_size
            print(f"WARNING: the dataset length ({len(self)}) is longer than memory_size ({self.memory_size})")

    @abstractmethod
    def append_buffer(self, buffer):
        """
        CAUTION: don't forget to append the info dictionary if you want to use CRC debugging.
        """
        raise NotImplementedError

    @abstractmethod
    def __len__(self):
        raise NotImplementedError

    @abstractmethod
    def get_transition(self, item):
        """
        Returns: tuple (prev_obs, prev_act(prev_obs), rew(prev_obs, prev_act), obs, done, info)
        info is required in each sample for CRC debugging. The 'crc' key is what is important when using this feature.
        Do NOT apply observation preprocessing here, as it will be applied automatically after this
        """
        raise NotImplementedError

    def append(self, buffer):
        if len(buffer) > 0:
            self.stat_train_return = buffer.stat_train_return
            self.stat_test_return = buffer.stat_test_return
            self.stat_train_steps = buffer.stat_train_steps
            self.stat_test_steps = buffer.stat_test_steps
            self.append_buffer(buffer)

    def __getitem__(self, item):
        prev_obs, new_act, rew, new_obs, done, info = self.get_transition(item)
        if self.crc_debug:
            po, a, o, r, d = info['crc_sample']
            check_samples_crc(po, a, o, r, d, prev_obs, new_act, new_obs, rew, done)
        if self.obs_preprocessor is not None:
            prev_obs = self.obs_preprocessor(prev_obs)
            new_obs = self.obs_preprocessor(new_obs)
        if self.sample_preprocessor is not None:
            prev_obs, new_act, rew, new_obs, done = self.sample_preprocessor(prev_obs, new_act, rew, new_obs, done)
        done = np.float32(done)  # we don't want bool tensors
        return prev_obs, new_act, rew, new_obs, done

    def sample_indices(self):
        return (randint(0, len(self) - 1) for _ in range(self.batchsize))

    def sample(self, indices=None):
        indices = self.sample_indices() if indices is None else indices
        batch = [self[idx] for idx in indices]
        batch = collate(batch, self.device)
        return batch


class TrajMemoryDataloading(MemoryDataloading, ABC):
    def __init__(self,
                 memory_size,
                 batchsize,
                 device,
                 path_loc,
                 remove_size=100,
                 obs_preprocessor: callable = None,
                 crc_debug=False,
                 traj_len=2):
        super().__init__(memory_size,
                         batchsize,
                         device,
                         path_loc,
                         remove_size,
                         obs_preprocessor,
                         sample_preprocessor=None,
                         crc_debug=crc_debug)
        self.traj_len = traj_len  # this must be used in __len__() and in get_trajectory()

    def get_transition(self, item):
        assert False, f"Invalid method for this class, implement get_trajectory instead."

    @abstractmethod
    def get_trajectory(self, item):
        """
        Returns: tuple (augm_obs_traj:list, rew_traj:list, done_traj:list, info_traj:list)
        each trajectory must be of length self.traj_len
        info_traj is required for CRC debugging. The 'crc' key is what is important when using this feature.
        Do NOT apply observation preprocessing here, as it will be applied automatically after this
        """
        raise NotImplementedError

    def __getitem__(self, item):
        augm_obs_traj, rew_traj, done_traj, info_traj = self.get_trajectory(item)
        assert len(augm_obs_traj) == len(rew_traj) == len(done_traj) == self.traj_len, f"all trajectories must be of length self.traj_len:{self.traj_len}."
        if self.crc_debug:
            for i in range(len(augm_obs_traj)):
                _, _, o, r, d = info_traj[i]['crc_sample']
                new_obs, rew, done = augm_obs_traj[i], rew_traj[i], done_traj[i]
                check_samples_crc_traj(o, r, d, new_obs, rew, done)
        if self.obs_preprocessor is not None:
            augm_obs_traj = [self.obs_preprocessor(obs) for obs in augm_obs_traj]
        if self.sample_preprocessor is not None:
            raise NotImplementedError("Sample preprocessing is not supported for trajectories.")
        done_traj = [np.float32(done) for done in done_traj]  # we don't want bool tensors
        return augm_obs_traj, rew_traj, done_traj


def load_and_print_pickle_file(path=r"C:\Users\Yann\Desktop\git\tmrl\data\data.pkl"):  # r"D:\data2020"
    import pickle
    with open(path, 'rb') as f:
        data = pickle.load(f)
    print(f"nb samples: {len(data[0])}")
    for i, d in enumerate(data):
        print(f"[{i}][0]: {d[0]}")
    print("full data:")
    for i, d in enumerate(data):
        print(f"[{i}]: {d}")


if __name__ == "__main__":
    load_and_print_pickle_file()