import numpy as np
import copy
import torch
from classifier_control.classifier.utils.general_utils import AttrDict
import torch.nn as nn
import pdb
from classifier_control.classifier.utils.subnetworks import ConvEncoder
from classifier_control.classifier.utils.spatial_softmax import SpatialSoftmax

from classifier_control.classifier.models.base_model import BaseModel
from classifier_control.classifier.utils.layers import Linear

from classifier_control.classifier.utils.mixup_regularization import MixupRegularizer


class SingleTempDistClassifier(BaseModel):
    def __init__(self, hp, tdist, logger):
        super().__init__(logger)
        self._hp = hp
        self.tdist = tdist
        self.build_network()

        if self._hp.use_mixup:
            self.mixup_reg = MixupRegularizer(self._hp.mixup_alpha)

    def build_network(self, build_encoder=True):
        self.encoder = ConvEncoder(self._hp)
        out_size = self.encoder.get_output_size()
        self.spatial_softmax = SpatialSoftmax(out_size[1], out_size[2], out_size[0])  # height, width, channel
        self.linear = Linear(in_dim=out_size[0]*2, out_dim=1, builder=self._hp.builder)

        self.cross_ent_loss = nn.BCEWithLogitsLoss()

    def forward(self, inputs):
        """
        forward pass at training time
        :param
            images shape = batch x time x channel x height x width
        :return: model_output
        """

        #import pdb; pdb.set_trace()
        tlen = inputs.demo_seq_images.shape[1]
        pos_pairs, neg_pairs = self.sample_image_pair(inputs.demo_seq_images, tlen, self.tdist)
        image_pairs = torch.cat([pos_pairs, neg_pairs], dim=0)
        embeddings = self.encoder(image_pairs)
        embeddings = self.spatial_softmax(embeddings)
        logits = self.linear(embeddings)
        self.out_sigmoid = torch.sigmoid(logits)
        model_output = AttrDict(logits=logits, out_sigmoid=self.out_sigmoid, pos_pair=self.pos_pair, neg_pair=self.neg_pair)
        return model_output


    def sample_mixup_pairs(self, images, tlen, tdist):

        # choose goal indices
        t1 = np.random.randint(tdist+1, tlen, self._hp.batch_size)

        # get positives:
        t0_pos = np.array([np.random.randint(t1[b]-tdist, t1[b], 1) for b in range(images.shape[0])]).squeeze()
        t0_pos_prime = np.array([np.random.randint(t1[b]-tdist, t1[b], 1) for b in range(images.shape[0])]).squeeze()
        t0_pos, t0_pos_prime, t1 = torch.from_numpy(t0_pos), torch.from_numpy(t0_pos_prime), torch.from_numpy(t1)

        # print('t0', t0)
        # print('t1', t1)
        # print('t1 - t0', t1 - t0)

        im_t0_pos = select_indices(images, t0_pos)
        im_t0_pos_prime = select_indices(images, t0_pos_prime)
        im_t1 = select_indices(images, t1)

        # get negatives:

        t0_neg = np.array([np.random.randint(0, t1[b]-tdist, 1) for b in range(images.shape[0])]).squeeze()
        t0_neg_prime = np.array([np.random.randint(0, t1[b]-tdist, 1) for b in range(images.shape[0])]).squeeze()
        t0_neg, t0_neg_prime = torch.from_numpy(t0_neg), torch.from_numpy(t0_neg_prime)

        # print('--------------')
        # print('t0', t0)
        # print('t1', t1)
        # print('t1 - t0', t1 - t0)

        im_t0_neg = select_indices(images, t0_neg)
        im_t0_neg_prime = select_indices(images, t0_neg_prime)

        total_images = torch.cat([im_t0_pos, im_t0_pos_prime, im_t0_neg, im_t0_neg_prime], dim=0)
        total_labels = torch.cat(2*[torch.ones(self._hp.batch_size)] + 2*[torch.zeros(self._hp.batch_size)], dim=0)

        random_indices = np.random.choice(total_images.shape[0], total_images.shape[0]//2, replace=False)  # Pick two indices to be one convex comb

        def get_cvx_comb_imgs_lbls(indices):
            comb_0 = total_images[indices]
            indices_comp = np.setdiff1d(np.arange(total_images.shape[0]), indices)
            comb_1 = total_images[indices_comp]
            comb, lam = self.mixup_reg(comb_0, comb_1)
            labels = self.mixup_reg.convex_comb(total_labels[indices].cuda(), total_labels[indices_comp].cuda(), lam)
            return comb, labels

        comb, labels = get_cvx_comb_imgs_lbls(random_indices)

        comb_1 = comb[:comb.shape[0]//2]
        comb_2 = comb[comb.shape[0]//2:]

        # Note that pos_pair and neg_pair don't really have any semantic meaning here anymore
        # They are just filled in so things don't break down the line
        self.pos_pair = torch.stack([comb_1, im_t1], dim=1)
        pos_pair_cat = torch.cat([comb_1, im_t1], dim=1)

        self.neg_pair = torch.stack([comb_2, im_t1], dim=1)
        neg_pair_cat = torch.cat([comb_2, im_t1], dim=1)

        # one means within range of tdist range,  zero means outside of tdist range
        self.labels = labels
        return pos_pair_cat, neg_pair_cat


    def sample_image_pair(self, images, tlen, tdist):

        if self._hp.use_mixup:
            return self.sample_mixup_pairs(images, tlen, tdist)

        # get positives:
        t0 = np.random.randint(0, tlen - tdist - 1, self._hp.batch_size)
        t1 = t0 + 1 + np.random.randint(0, tdist, self._hp.batch_size)
        t0, t1 = torch.from_numpy(t0), torch.from_numpy(t1)

        # print('t0', t0)
        # print('t1', t1)
        # print('t1 - t0', t1 - t0)

        im_t0 = select_indices(images, t0)
        im_t1 = select_indices(images, t1)

        self.pos_pair = torch.stack([im_t0, im_t1], dim=1)
        pos_pair_cat = torch.cat([im_t0, im_t1], dim=1)

        # get negatives:
        t0 = np.random.randint(0, tlen - tdist - 1, self._hp.batch_size)
        t1 = [np.random.randint(t0[b] + tdist + 1, tlen, 1) for b in range(self._hp.batch_size)]
        t1 = np.array(t1).squeeze()
        t0, t1 = torch.from_numpy(t0), torch.from_numpy(t1)

        # print('--------------')
        # print('t0', t0)
        # print('t1', t1)
        # print('t1 - t0', t1 - t0)

        im_t0 = select_indices(images, t0)
        im_t1 = select_indices(images, t1)
        self.neg_pair = torch.stack([im_t0, im_t1], dim=1)
        neg_pair_cat = torch.cat([im_t0, im_t1], dim=1)

        # one means within range of tdist range,  zero means outside of tdist range
        self.labels = torch.cat([torch.ones(self._hp.batch_size), torch.zeros(self._hp.batch_size)])

        return pos_pair_cat, neg_pair_cat

    def loss(self, model_output):
        logits_ = model_output.logits[:, 0]
        return self.cross_ent_loss(logits_, self.labels.to(self._hp.device))

    def _log_outputs(self, model_output, inputs, losses, step, log_images, phase):

        out_sigmoid = self.out_sigmoid.data.cpu().numpy().squeeze()
        predictions = np.zeros(out_sigmoid.shape)
        predictions[np.where(out_sigmoid > 0.5)] = 1

        labels = self.labels.data.cpu().numpy()

        num_neg = np.sum(labels == 0)
        false_positive_rate = np.sum(predictions[np.where(labels == 0)])/float(num_neg)

        num_pos = np.sum(labels == 1)
        false_negative_rate = np.sum(1-predictions[np.where(labels == 1)])/float(num_pos)

        self._logger.log_scalar(false_positive_rate, 'tdist{}_false_postive_rate'.format(self.tdist), step, phase)
        self._logger.log_scalar(false_negative_rate, 'tdist{}_false_negative_rate'.format(self.tdist), step, phase)

        if log_images:
            self._logger.log_single_tdist_classifier_image(self.pos_pair, self.neg_pair, self.out_sigmoid,
                                                          'tdist{}'.format(self.tdist), step, phase)


def select_indices(tensor, indices):
    new_images = []
    for b in range(tensor.shape[0]):
        new_images.append(tensor[b, indices[b]])
    tensor = torch.stack(new_images, dim=0)
    return tensor

class TesttimeSingleTempDistClassifier(SingleTempDistClassifier):
    def __init__(self, params, tdist, logger):
        super().__init__(params, tdist, logger)

    def forward(self, inputs):
        """
        forward pass at training time
        :param
            images shape = batch x channel x height x width
        :return: model_output
        """

        image_pairs = torch.cat([inputs['current_img'], inputs['goal_img']], dim=1)
        embeddings = self.encoder(image_pairs)
        embeddings = self.spatial_softmax(embeddings)
        logits = self.linear(embeddings)
        self.out_sigmoid = torch.sigmoid(logits)
        model_output = AttrDict(logits=logits, out_sigmoid=self.out_sigmoid)
        return model_output



