# -*- coding: utf-8 -*-
"""Main module."""

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.distributions import Normal, Multinomial, kl_divergence as kl

from scvi.models.log_likelihood import (
    log_zinb_positive,
    log_nb_positive,
    log_beta_bernoulli,
    log_zero_inflated_bernoulli,
)
from scvi.models.modules import Encoder, DecoderSCVI, Decoder, LinearDecoderSCVI
from scvi.models.utils import one_hot

import numpy as np

torch.backends.cudnn.benchmark = True


# VAE model
class VAE_ATAC(nn.Module):
    r"""Variational auto-encoder model.

    :param n_input: Number of input genes
    :param n_batch: Number of batches
    :param n_labels: Number of labels
    :param n_hidden: Number of nodes per hidden layer
    :param n_latent: Dimensionality of the latent space
    :param n_layers: Number of hidden layers used for encoder and decoder NNs
    :param dropout_rate: Dropout rate for neural networks
    :param dispersion: One of the following

        * ``'gene'`` - dispersion parameter of NB is constant per gene across cells
        * ``'gene-batch'`` - dispersion can differ between different batches
        * ``'gene-label'`` - dispersion can differ between different labels
        * ``'gene-cell'`` - dispersion can differ for every gene in every cell

    :param log_variational: Log variational distribution
    :param reconstruction_loss:  One of

        * ``'nb'`` - Negative binomial distribution
        * ``'zinb'`` - Zero-inflated negative binomial distribution

    Examples:
        >>> gene_dataset = CortexDataset()
        >>> vae = VAE(gene_dataset.nb_genes, n_batch=gene_dataset.n_batches * False,
        ... n_labels=gene_dataset.n_labels)

    """

    def __init__(
        self,
        n_input: int,
        n_batch: int = 0,
        n_labels: int = 0,
        n_hidden: int = 128,
        n_latent: int = 10,
        n_layers: int = 1,
        dropout_rate: float = 0.1,
        dispersion: str = "gene",
        log_variational: bool = False,
        reconstruction_loss: str = "lda",
        log_alpha_prior=None,
    ):
        super().__init__()
        self.dispersion = dispersion
        self.n_latent = n_latent
        self.log_variational = log_variational
        self.reconstruction_loss = reconstruction_loss
        # Automatically deactivate if useless
        self.n_batch = n_batch
        self.n_labels = n_labels
        self.n_latent_layers = 1  # not sure what this is for, no usages?

        if log_alpha_prior is None and reconstruction_loss == "lda":
            self.l_alpha_prior = torch.nn.Parameter(torch.randn(1))
        elif type(log_alpha_prior) is not str and reconstruction_loss == "lda":
            self.l_alpha_prior = torch.tensor(log_alpha_prior)
        else:
            self.l_alpha_prior = None

        if self.dispersion == "gene":
            self.px_r = torch.nn.Parameter(torch.randn(n_input))
        elif self.dispersion == "gene-batch":
            self.px_r = torch.nn.Parameter(torch.randn(n_input, n_batch))
        elif self.dispersion == "gene-label":
            self.px_r = torch.nn.Parameter(torch.randn(n_input, n_labels))
        else:  # gene-cell
            pass

        # z encoder goes from the n_input-dimensional data to an n_latent-d
        # latent space representation
        if reconstruction_loss != "lda":
            self.z_encoder = Encoder(
                n_input,
                n_latent,
                n_layers=n_layers,
                n_hidden=n_hidden,
                dropout_rate=dropout_rate,
            )
        else:
            self.z_encoder = Encoder(
                n_input,
                n_latent,
                n_layers=n_layers,
                n_hidden=n_hidden,
                dropout_rate=dropout_rate,
                distribution="ln",
            )
        # l encoder goes from n_input-dimensional data to 1-d library size
        self.l_encoder = Encoder(
            n_input, 1, n_layers=1, n_hidden=n_hidden, dropout_rate=dropout_rate
        )
        # decoder goes from n_latent-dimensional space to n_input-d data
        if reconstruction_loss not in [
            "beta-bernoulli",
            "zero_inflated_bernoulli",
            "bernoulli",
            "multinomial",
            "lda",
        ]:
            self.decoder = DecoderSCVI(
                n_latent,
                n_input,
                n_cat_list=[n_batch],
                n_layers=n_layers,
                n_hidden=n_hidden,
            )
        elif reconstruction_loss == "lda":
            self.decoder = LinearDecoderSCVI(n_latent, n_input, n_cat_list=[n_batch])
        else:
            self.decoder = Decoder(
                n_latent,
                n_input,
                n_cat_list=[n_batch],
                n_layers=n_layers,
                n_hidden=n_hidden,
                dropout_rate=dropout_rate,
            )

    def get_latents(self, x, y=None):
        r""" returns the result of ``sample_from_posterior_z`` inside a list

        :param x: tensor of values with shape ``(batch_size, n_input)``
        :param y: tensor of cell-types labels with shape ``(batch_size, n_labels)``
        :return: one element list of tensor
        :rtype: list of :py:class:`torch.Tensor`
        """
        return [self.sample_from_posterior_z(x, y)]

    def sample_from_posterior_z(self, x, y=None, give_mean=False):
        r""" samples the tensor of latent values from the posterior
        #doesn't really sample, returns the means of the posterior distribution

        :param x: tensor of values with shape ``(batch_size, n_input)``
        :param y: tensor of cell-types labels with shape ``(batch_size, n_labels)``
        :param give_mean: is True when we want the mean of the posterior  distribution rather than sampling
        :return: tensor of shape ``(batch_size, n_latent)``
        :rtype: :py:class:`torch.Tensor`
        """
        if self.log_variational:
            x = torch.log(1 + x)
        qz_m, qz_v, z = self.z_encoder(x, y)  # y only used in VAEC
        if give_mean:
            z = qz_m
        return z

    def sample_from_posterior_l(self, x):
        r""" samples the tensor of library sizes from the posterior
        #doesn't really sample, returns the tensor of the means of the posterior distribution

        :param x: tensor of values with shape ``(batch_size, n_input)``
        :param y: tensor of cell-types labels with shape ``(batch_size, n_labels)``
        :return: tensor of shape ``(batch_size, 1)``
        :rtype: :py:class:`torch.Tensor`
        """
        if self.log_variational:
            x = torch.log(1 + x)
        ql_m, ql_v, library = self.l_encoder(x)
        return library

    def get_sample_scale(self, x, batch_index=None, y=None, n_samples=1):
        r"""Returns the tensor of predicted frequencies of expression

        :param x: tensor of values with shape ``(batch_size, n_input)``
        :param batch_index: array that indicates which batch the cells belong to with shape ``batch_size``
        :param y: tensor of cell-types labels with shape ``(batch_size, n_labels)``
        :param n_samples: number of samples
        :return: tensor of predicted frequencies of expression with shape ``(batch_size, n_input)``
        :rtype: :py:class:`torch.Tensor`
        """
        return self.inference(x, batch_index=batch_index, y=y, n_samples=n_samples)[0]

    def get_sample_rate(self, x, batch_index=None, y=None, n_samples=1):
        r"""Returns the tensor of means of the negative binomial distribution

        :param x: tensor of values with shape ``(batch_size, n_input)``
        :param y: tensor of cell-types labels with shape ``(batch_size, n_labels)``
        :param batch_index: array that indicates which batch the cells belong to with shape ``batch_size``
        :param n_samples: number of samples
        :return: tensor of means of the negative binomial distribution with shape ``(batch_size, n_input)``
        :rtype: :py:class:`torch.Tensor`
        """
        return self.inference(x, batch_index=batch_index, y=y, n_samples=n_samples)[2]

    def _reconstruction_loss(self, x, px_rate, px_r, px_dropout, alpha, beta):
        # Reconstruction Loss
        if self.reconstruction_loss == "zinb":
            reconst_loss = -log_zinb_positive(x, px_rate, px_r, px_dropout)
        elif self.reconstruction_loss == "nb":
            reconst_loss = -log_nb_positive(x, px_rate, px_r)
        elif self.reconstruction_loss == "beta-bernoulli":
            reconst_loss = -log_beta_bernoulli(x, alpha, beta)
        elif self.reconstruction_loss == "bernoulli":
            reconst_loss = -torch.sum(torch.log(x * beta + (1 - x) * (1 - beta)), dim=1)
        elif self.reconstruction_loss == "zero_inflated_bernoulli":
            reconst_loss = -log_zero_inflated_bernoulli(x, beta, alpha)
        else:
            # reconst_loss = -Multinomial(probs=torch.t(alpha)).log_prob(x)
            reconst_loss = -Multinomial(probs=alpha).log_prob(x)

        return reconst_loss

    def scale_from_z(self, sample_batch, fixed_batch):
        if self.log_variational:
            sample_batch = torch.log(1 + sample_batch)
        qz_m, qz_v, z = self.z_encoder(sample_batch)
        batch_index = fixed_batch * torch.ones_like(sample_batch[:, [0]])
        library = 4.0 * torch.ones_like(sample_batch[:, [0]])
        px_scale, _, _, _ = self.decoder("gene", z, library, batch_index)
        return px_scale

    def inference(self, x, batch_index=None, y=None, n_samples=1):
        x_ = x
        if self.log_variational:
            x_ = torch.log(1 + x_)

        # Sampling
        qz_m, qz_v, z = self.z_encoder(x_, y)
        ql_m, ql_v, library = self.l_encoder(x_)

        if n_samples > 1:
            qz_m = qz_m.unsqueeze(0).expand((n_samples, qz_m.size(0), qz_m.size(1)))
            qz_v = qz_v.unsqueeze(0).expand((n_samples, qz_v.size(0), qz_v.size(1)))
            z = Normal(qz_m, qz_v.sqrt()).sample()
            ql_m = ql_m.unsqueeze(0).expand((n_samples, ql_m.size(0), ql_m.size(1)))
            ql_v = ql_v.unsqueeze(0).expand((n_samples, ql_v.size(0), ql_v.size(1)))
            library = Normal(ql_m, ql_v.sqrt()).sample()

        if self.reconstruction_loss not in [
            "beta-bernoulli",
            "zero_inflated_bernoulli",
            "bernoulli",
            "multinomial",
            "lda",
        ]:
            px_scale, px_r, px_rate, px_dropout = self.decoder(
                self.dispersion, z, library, batch_index, y
            )
            if self.dispersion == "gene-label":
                px_r = F.linear(
                    one_hot(y, self.n_labels), self.px_r
                )  # px_r gets transposed - last dimension is nb genes
            elif self.dispersion == "gene-batch":
                px_r = F.linear(one_hot(batch_index, self.n_batch), self.px_r)
            elif self.dispersion == "gene":
                px_r = self.px_r
            px_r = torch.exp(px_r)
            alpha = None
            beta = None
        elif self.reconstruction_loss == "beta-bernoulli":
            log_alpha, beta = self.decoder(z, batch_index, y)
            alpha = torch.exp(log_alpha)
            (px_scale, px_r, px_rate, px_dropout) = (None, None, None, None)
        elif self.reconstruction_loss in ["bernoulli", "zero_inflated_bernoulli"]:
            # alpha is dropout
            alpha, beta = self.decoder(z, batch_index, y)
            beta = torch.sigmoid(torch.log(beta))
            (px_scale, px_r, px_rate, px_dropout) = (None, None, None, None)
        elif self.reconstruction_loss == "lda":
            alpha = self.decoder(self.dispersion, z, library, batch_index, y)
            alpha = F.softmax(alpha, dim=1)
            (px_scale, px_r, px_rate, px_dropout, beta) = (None, None, None, None, None)
        else:
            log_alpha, beta = self.decoder(z, batch_index, y)
            alpha = F.softmax(log_alpha, dim=1)
            (px_scale, px_r, px_rate, px_dropout) = (None, None, None, None)

        return (
            px_scale,
            px_r,
            px_rate,
            px_dropout,
            qz_m,
            qz_v,
            z,
            ql_m,
            ql_v,
            library,
            alpha,
            beta,
        )

    def forward(self, x, local_l_mean, local_l_var, batch_index=None, y=None):
        r""" Returns the reconstruction loss and the Kullback divergences

        :param x: tensor of values with shape (batch_size, n_input)
        :param local_l_mean: tensor of means of the prior distribution of latent variable l
         with shape (batch_size, 1)
        :param local_l_var: tensor of variancess of the prior distribution of latent variable l
         with shape (batch_size, 1)
        :param batch_index: array that indicates which batch the cells belong to with shape ``batch_size``
        :param y: tensor of cell-types labels with shape (batch_size, n_labels)
        :return: the reconstruction loss and the Kullback divergences
        :rtype: 2-tuple of :py:class:`torch.FloatTensor`
        """
        # Parameters for z latent distribution

        px_scale, px_r, px_rate, px_dropout, qz_m, qz_v, z, ql_m, ql_v, library, alpha, beta = self.inference(
            x, batch_index, y
        )

        # KL Divergence
        ap = self.l_alpha_prior
        if ap is None:
            mean = torch.zeros_like(qz_m)
            scale = torch.ones_like(qz_v)
        else:
            mean = ap - (1 / self.n_latent) * (self.n_latent * ap)
            scale = torch.sqrt(
                (1 / torch.exp(ap)) * (1 - 2 / self.n_latent)
                + (1 / self.n_latent ** 2) * (self.n_latent * 1 / torch.exp(ap))
            )

        kl_divergence_z = kl(Normal(qz_m, torch.sqrt(qz_v)), Normal(mean, scale)).sum(
            dim=1
        )
        if self.reconstruction_loss not in [
            "beta-bernoulli",
            "zero_inflated_bernoulli",
            "bernoulli",
            "multinomial",
            "lda",
        ]:
            kl_divergence_l = kl(
                Normal(ql_m, torch.sqrt(ql_v)),
                Normal(local_l_mean, torch.sqrt(local_l_var)),
            ).sum(dim=1)
        else:
            kl_divergence_l = 0
        kl_divergence = kl_divergence_z

        reconst_loss = self._reconstruction_loss(
            x, px_rate, px_r, px_dropout, alpha, beta
        )

        return reconst_loss + kl_divergence_l, kl_divergence
