from __future__ import absolute_import

from torch import nn
import torch
from .modules import FixupConvModule, FixupResidualChain
from dataclasses import dataclass


@dataclass
class FixUpUnetConfig:
    feat: int = 64
    in_feat: int = 3
    out_feat: int = 3
    down_layers: int = 5
    identity_layers: int = 3
    bottleneck_layers: int = 6
    skips: bool = True
    act_fn: str = "relu"
    out_act_fn: str = "none"
    max_feat: int = 512
    script_submodules: bool = True
    dim: int = 2

    def init(self):
        return FixUpUnet(self)

class FixUpUnet(nn.Module):
    """
    Unet using residual blocks and residual chains without any normalization layer.
    """

    def __init__(self, cfg: FixUpUnetConfig):
        super(FixUpUnet, self).__init__()

        feat = cfg.feat
        self.skip = cfg.skips
        max_feat = cfg.max_feat

        i = -1

        layer = FixupConvModule(
            cfg.in_feat, cfg.feat, 3, 1, True, "none", cfg.act_fn, dim=cfg.dim
        )
        if cfg.script_submodules:
            layer = torch.jit.script(layer)
        self.in_conv = layer

        self.down_layers = nn.ModuleList()
        for i in range(cfg.down_layers):
            feat_curr = min(2**i * feat, max_feat)
            feat_next = min(2 ** (i + 1) * feat, max_feat)
            # Residual chain
            layer = FixupResidualChain(
                feat_curr,
                cfg.identity_layers,
                3,
                cfg.act_fn,
                depth_init=2 * cfg.identity_layers,
                single_padding=(i < 3),
                dim=cfg.dim,
            )
            if cfg.script_submodules:
                layer = torch.jit.script(layer)
            self.down_layers.append(layer)

            # Downsampling convolution
            layer = FixupConvModule(
                feat_curr,
                feat_next,
                4,
                2,
                True,
                "none",
                cfg.act_fn,
                use_bias=True,
                dim=cfg.dim,
            )
            if cfg.script_submodules:
                layer = torch.jit.script(layer)
            self.down_layers.append(layer)

        self.bottleneck_layers = nn.ModuleList()
        feat_curr = min(2 ** (i + 1) * feat, max_feat)
        layer = FixupResidualChain(
            feat_curr, cfg.bottleneck_layers, 3, cfg.act_fn, dim=cfg.dim
        )
        if cfg.script_submodules:
            layer = torch.jit.script(layer)
        self.bottleneck_layers.append(layer)

        self.up_layers = nn.ModuleList()
        upsample_mode = "bilinear" if cfg.dim == 2 else "trilinear"
        for i in range(cfg.down_layers, 0, -1):
            feat_curr = min(2**i * feat, max_feat)
            feat_next = min(2 ** (i - 1) * feat, max_feat)
            # Upsample
            self.up_layers.append(
                CastUpsample()
            )
            # Eventually merge skip and upsample
            feat_inter = feat_next + feat_curr if self.skip else feat_curr
            layer = FixupConvModule(
                feat_inter,
                feat_next,
                1,
                1,
                False,
                "none",
                cfg.act_fn,
                use_bias=True,
                dim=cfg.dim,
            )
            if cfg.script_submodules:
                layer = torch.jit.script(layer)
            self.up_layers.append(layer)
            # Residual chain
            layer = FixupResidualChain(
                feat_next,
                cfg.identity_layers,
                3,
                cfg.act_fn,
                depth_init=2 * cfg.identity_layers,
                single_padding=(i - 1 < 3),
                dim=cfg.dim,
            )
            if cfg.script_submodules:
                layer = torch.jit.script(layer)
            self.up_layers.append(layer)

        layer = FixupConvModule(
            feat,
            cfg.out_feat,
            3,
            1,
            True,
            "none",
            cfg.out_act_fn,
            use_bias=True,
            dim=cfg.dim,
        )
        if cfg.script_submodules:
            layer = torch.jit.script(layer)
        self.out_conv = layer

    def forward(self, x):

        skips = []
        x = self.in_conv(x)

        for i, layer in enumerate(self.down_layers):
            x = layer(x)

            if i % 2 == 0:
                skips.append(x)

        for layer in self.bottleneck_layers:
            x = layer(x)

        for i, layer in enumerate(self.up_layers):
            x = layer(x)

            if self.skip:
                if i % 3 == 0:
                    x = torch.cat([x, skips.pop()], dim=1)

        return self.out_conv(x)


class CastUpsample(nn.Module):
    def __init__(self):
        super().__init__()
        self.upsample = nn.Upsample(scale_factor=2, mode="bilinear", align_corners=False).float()

    def forward(self, x):
        return self.upsample(x.float()).to(x.dtype)