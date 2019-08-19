import chainer
from chainer.backends import cuda
import chainer.functions as F
import chainer.links as L
import numpy as np

import objslampp

from .pspnet import PSPNetExtractor


class BaselineModel(chainer.Chain):

    _models = objslampp.datasets.YCBVideoModels()
    _voxel_dim = 32

    def __init__(
        self,
        *,
        n_fg_class,
        loss=None,
        loss_scale=None,
    ):
        super().__init__()

        self._n_fg_class = n_fg_class

        self._loss = 'add/add_s' if loss is None else loss
        assert self._loss in [
            'add',
            'add_s',
            'add/add_s',
            'add+add_s',
        ]

        if loss_scale is None:
            loss_scale = {
                'add+add_s': 0.5,
            }
        self._loss_scale = loss_scale

        with self.init_scope():
            # extractor
            self.resnet_extractor = objslampp.models.ResNet18()
            self.pspnet_extractor = PSPNetExtractor()
            self.voxel_extractor = VoxelFeatureExtractor()

            self.fc1_rot = L.Linear(1024, 640, 1)
            self.fc1_trans = L.Linear(1024, 640, 1)
            self.fc2_rot = L.Linear(640, 256, 1)
            self.fc2_trans = L.Linear(640, 256, 1)
            self.fc3_rot = L.Linear(256, 128, 1)
            self.fc3_trans = L.Linear(256, 128, 1)
            self.fc4_rot = L.Linear(128, n_fg_class * 4, 1)
            self.fc4_trans = L.Linear(128, n_fg_class * 3, 1)

    def predict(
        self,
        *,
        class_id,
        pitch,
        origin,
        rgb,
        pcd,
        quaternion_true=None,
        translation_true=None,
    ):
        xp = self.xp

        B, H, W, C = rgb.shape
        assert H == W == 256
        assert C == 3
        dimensions = (self._voxel_dim,) * 3

        # prepare
        pitch = pitch.astype(np.float32)
        origin = origin.astype(np.float32)
        rgb = rgb.transpose(0, 3, 1, 2).astype(np.float32)  # BHWC -> BCHW
        pcd = pcd.transpose(0, 3, 1, 2).astype(np.float32)  # BHW3 -> B3HW
        quaternion_true = quaternion_true.astype(np.float32)
        translation_true = translation_true.astype(np.float32)

        # feature extraction
        mean = xp.asarray(self.resnet_extractor.mean)
        h_rgb = self.resnet_extractor(rgb - mean[None])  # 1/8
        h_rgb = self.pspnet_extractor(h_rgb)  # 1/1

        h = []
        actives = []
        for i in range(B):
            h_rgb_i = h_rgb[i]
            pcd_i = pcd[i]

            h_rgb_i = h_rgb_i.transpose(1, 2, 0)      # CHW -> HWC
            pcd_i = pcd_i.transpose(1, 2, 0)  # 3HW -> HW3
            mask_i = ~xp.isnan(pcd_i).any(axis=2)

            values = h_rgb_i[mask_i, :]    # MC
            points = pcd_i[mask_i, :]  # M3

            h_i, counts_i = objslampp.functions.average_voxelization_3d(
                values=values,
                points=points,
                origin=origin[i],
                pitch=pitch[i],
                dimensions=dimensions,
                channels=h_rgb_i.shape[2],
                return_counts=True,
            )  # CXYZ
            actives_i = counts_i[0] > 0

            if chainer.config.train:
                T_cad2cam_i = objslampp.functions.quaternion_matrix(
                    quaternion_true[i][None]
                )[0]
                T_cad2cam_i = objslampp.functions.compose_transform(
                    T_cad2cam_i[:3, :3][None], translation_true[i][None]
                )[0]

                indices = xp.where(class_id[i])[0]
                indices = indices[indices != i].tolist()
                if len(indices) > 0:
                    n_fuse = np.random.randint(0, len(indices) + 1)
                    if n_fuse > 0:
                        indices = np.random.choice(
                            indices, n_fuse, replace=False
                        )
                for j in indices:
                    h_rgb_j = h_rgb[j]
                    pcd_j = pcd[j]

                    h_rgb_j = h_rgb_j.transpose(1, 2, 0)
                    pcd_j = pcd_j.transpose(1, 2, 0)
                    mask_j = ~xp.isnan(pcd_j).any(axis=2)

                    values = h_rgb_j[mask_j, :]
                    points = pcd_j[mask_j, :]

                    T_cad2cam_j = objslampp.functions.quaternion_matrix(
                        quaternion_true[j][None]
                    )[0]
                    T_cad2cam_j = objslampp.functions.compose_transform(
                        T_cad2cam_j[:3, :3][None], translation_true[j][None]
                    )[0]
                    points = objslampp.functions.transform_points(
                        points, F.inv(T_cad2cam_j)[None]
                    )[0]
                    points = objslampp.functions.transform_points(
                        points, T_cad2cam_i[None]
                    )[0]

                    h_j, counts_j = objslampp.functions.average_voxelization_3d(  # NOQA
                        values=values,
                        points=points,
                        origin=origin[i],
                        pitch=pitch[i],
                        dimensions=dimensions,
                        channels=h_rgb_i.shape[2],
                        return_counts=True,
                    )  # CXYZ

                    h_i = F.maximum(h_i, h_j)
                    actives_i = actives_i | (counts_j[0] > 0)

            h.append(h_i[None])
            actives.append(actives_i[None])
        h = F.concat(h, axis=0)           # BCXYZ
        actives = xp.concatenate(actives, axis=0)  # BXYZ

        centroids = []
        for i in range(B):
            # mean of active points (voxels)
            centroid = xp.stack(xp.where(actives[i])).mean(axis=1)
            centroid = centroid * pitch[i] + origin[i]
            centroids.append(centroid[None])
        centroids = xp.concatenate(centroids, axis=0)  # B3

        h = self.voxel_extractor(h, actives)

        h_rot = F.relu(self.fc1_rot(h))
        h_trans = F.relu(self.fc1_trans(h))
        h_rot = F.relu(self.fc2_rot(h_rot))
        h_trans = F.relu(self.fc2_trans(h_trans))
        h_rot = F.relu(self.fc3_rot(h_rot))
        h_trans = F.relu(self.fc3_trans(h_trans))
        cls_rot = self.fc4_rot(h_rot)
        cls_trans = self.fc4_trans(h_trans)

        quaternion = cls_rot.reshape(B, self._n_fg_class, 4)
        translation = cls_trans.reshape(B, self._n_fg_class, 3)

        fg_class_id = class_id - 1
        quaternion = quaternion[xp.arange(B), fg_class_id, :]
        translation = translation[xp.arange(B), fg_class_id, :]

        quaternion = F.normalize(quaternion, axis=1)
        translation = centroids + translation * pitch[:, None]

        return quaternion, translation

    def __call__(
        self,
        *,
        class_id,
        pitch,
        origin,
        rgb,
        pcd,
        quaternion_true,
        translation_true,
    ):
        keep = class_id != -1
        if keep.sum() == 0:
            return chainer.Variable(self.xp.zeros((), dtype=np.float32))

        class_id = class_id[keep]
        pitch = pitch[keep]
        origin = origin[keep]
        rgb = rgb[keep]
        pcd = pcd[keep]
        quaternion_true = quaternion_true[keep]
        translation_true = translation_true[keep]

        quaternion_pred, translation_pred = self.predict(
            class_id=class_id,
            pitch=pitch,
            origin=origin,
            rgb=rgb,
            pcd=pcd,
            quaternion_true=quaternion_true,
            translation_true=translation_true,
        )

        self.evaluate(
            class_id=class_id,
            quaternion_true=quaternion_true,
            translation_true=translation_true,
            quaternion_pred=quaternion_pred,
            translation_pred=translation_pred,
        )

        loss = self.loss(
            class_id=class_id,
            quaternion_true=quaternion_true,
            translation_true=translation_true,
            quaternion_pred=quaternion_pred,
            translation_pred=translation_pred,
        )
        return loss

    def evaluate(
        self,
        *,
        class_id,
        quaternion_true,
        translation_true,
        quaternion_pred,
        translation_pred,
    ):
        quaternion_true = quaternion_true.astype(np.float32)
        translation_true = translation_true.astype(np.float32)

        batch_size = class_id.shape[0]

        T_cad2cam_true = objslampp.functions.quaternion_matrix(quaternion_true)
        T_cad2cam_pred = objslampp.functions.quaternion_matrix(quaternion_pred)
        T_cad2cam_true = objslampp.functions.compose_transform(
            Rs=T_cad2cam_true[:, :3, :3], ts=translation_true,
        )
        T_cad2cam_pred = objslampp.functions.compose_transform(
            Rs=T_cad2cam_pred[:, :3, :3], ts=translation_pred,
        )
        T_cad2cam_true = cuda.to_cpu(T_cad2cam_true.array)
        T_cad2cam_pred = cuda.to_cpu(T_cad2cam_pred.array)

        summary = chainer.DictSummary()
        for i in range(batch_size):
            class_id_i = int(class_id[i])
            cad_pcd = self._models.get_pcd(class_id=class_id_i)
            add, add_s = objslampp.metrics.average_distance(
                [cad_pcd], [T_cad2cam_true[i]], [T_cad2cam_pred[i]]
            )
            add, add_s = add[0], add_s[0]
            if chainer.config.train:
                summary.add({'add': add, 'add_s': add_s})
            else:
                summary.add({
                    f'add/{class_id_i:04d}': add,
                    f'add_s/{class_id_i:04d}': add_s,
                })
        chainer.report(summary.compute_mean(), self)

    def loss(
        self,
        *,
        class_id,
        quaternion_true,
        translation_true,
        quaternion_pred,
        translation_pred,
    ):
        quaternion_true = quaternion_true.astype(np.float32)
        translation_true = translation_true.astype(np.float32)

        R_cad2cam_true = objslampp.functions.quaternion_matrix(quaternion_true)
        R_cad2cam_pred = objslampp.functions.quaternion_matrix(quaternion_pred)
        del quaternion_true
        del quaternion_pred

        T_cad2cam_true = objslampp.functions.compose_transform(
            R_cad2cam_true[:, :3, :3], translation_true,
        )
        T_cad2cam_pred = objslampp.functions.compose_transform(
            R_cad2cam_pred[:, :3, :3], translation_pred,
        )
        del translation_true
        del translation_pred
        del R_cad2cam_true
        del R_cad2cam_pred

        batch_size = class_id.shape[0]

        loss = 0
        for i in range(batch_size):
            class_id_i = int(class_id[i])

            if self._loss in [
                'add',
                'add_s',
                'add/add_s',
                'add+add_s',
            ]:
                if self._loss in ['add+add_s']:
                    is_symmetric = None
                elif self._loss in ['add']:
                    is_symmetric = False
                elif self._loss in ['add_s']:
                    is_symmetric = True
                else:
                    assert self._loss in ['add/add_s']
                    is_symmetric = class_id_i in \
                        objslampp.datasets.ycb_video.class_ids_symmetric
                cad_pcd = self._models.get_pcd(class_id=class_id_i)
                cad_pcd = self.xp.asarray(cad_pcd, dtype=np.float32)

            if self._loss in [
                'add',
                'add_s',
                'add/add_s',
            ]:
                assert is_symmetric in [True, False]
                loss_i = objslampp.functions.average_distance_l1(
                    points=cad_pcd,
                    transform1=T_cad2cam_true[i][None],
                    transform2=T_cad2cam_pred[i][None],
                    symmetric=is_symmetric,
                )[0]
            elif self._loss in ['add+add_s']:
                kwargs = dict(
                    points=cad_pcd,
                    transform1=T_cad2cam_true[i][None],
                    transform2=T_cad2cam_pred[i][None],
                )
                loss_add_i = objslampp.functions.average_distance_l1(
                    **kwargs, symmetric=False
                )[0]
                loss_add_s_i = objslampp.functions.average_distance_l1(
                    **kwargs, symmetric=True
                )[0]
                loss_i = (
                    self._loss_scale['add+add_s'] * loss_add_i +
                    (1 - self._loss_scale['add+add_s']) * loss_add_s_i
                )
            else:
                raise ValueError(f'unsupported loss: {self._loss}')

            loss += loss_i
        loss /= batch_size

        values = {'loss': loss}
        chainer.report(values, observer=self)

        return loss


class VoxelFeatureExtractor(chainer.Chain):

    def __init__(self):
        super().__init__()
        with self.init_scope():
            self.conv1 = L.Convolution3D(None, 128, 4, stride=2, pad=1)
            self.conv2 = L.Convolution3D(128, 256, 3, stride=1, pad=1)
            self.conv3 = L.Convolution3D(256, 512, 3, stride=1, pad=1)
            self.conv4 = L.Convolution3D(512, 1024, 3, stride=1, pad=1)

    def __call__(self, h, actives):
        xp = self.xp

        h = F.relu(self.conv1(h))
        h = F.relu(self.conv2(h))
        h = F.relu(self.conv3(h))
        h = F.relu(self.conv4(h))

        h_ = []
        for i in range(h.shape[0]):
            X, Y, Z = xp.where(actives[i, :, :, :])
            X, Y, Z = X // 2, Y // 2, Z // 2
            h_i = h[i, :, X, Y, Z]
            h_i = F.average(h_i, axis=0)
            h_.append(h_i[None])
        h = F.concat(h_, axis=0)
        del h_

        return h
