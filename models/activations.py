# Activation functions

import torch
import torch.nn as nn
import torch.nn.functional as F


class SiLU(nn.Module):
    # SiLU/Swish
    # https://arxiv.org/pdf/1606.08415.pdf
    @staticmethod
    def forward(x):
        return x * torch.sigmoid(x)  # 默认参数 = 1

class MemoryEfficientSwish(nn.Module):
    # 节省内存的Swish 不采用自动求导(自己写前向传播和反向传播) 更高效
    class F(torch.autograd.Function):
        @staticmethod
        def forward(ctx, x):
            # save_for_backward会保留x的全部信息(一个完整的外挂Autograd Function的Variable),
            # 并提供避免in-place操作导致的input在backward被修改的情况.
            # in-place操作指不通过中间变量计算的变量间的操作。
            ctx.save_for_backward(x)
            return x * torch.sigmoid(x)

        @staticmethod
        def backward(ctx, grad_output):
            # 此处saved_tensors[0] 作用同上文 save_for_backward
            x = ctx.saved_tensors[0]
            sx = torch.sigmoid(x)
            # 返回该激活函数求导之后的结果 求导过程见上文
            return grad_output * (sx * (1 + x * (1 - sx)))

    def forward(self, x): # 应用前向传播方法
        return self.F.apply(x)

class Hardswish(nn.Module):
    """
    hard-swish 图形和Swish很相似 在mobilenet v3中提出
    https://arxiv.org/pdf/1905.02244.pdf
    """
    @staticmethod
    def forward(x):
        # return x * F.hardsigmoid(x)  # for torchscript and CoreML
        return x * F.hardtanh(x + 3, 0., 6.) / 6.  # for torchscript, CoreML and ONNX

class Mish(nn.Module):
    """
    Mish 激活函数
    https://arxiv.org/pdf/1908.08681.pdf
    https://github.com/digantamisra98/Mish/blob/master/Mish/Torch/mish.py
    """
    @staticmethod
    def forward(x):
        return x * F.softplus(x).tanh()  # softplus(x) = ln(1 + exp(x)

class MemoryEfficientMish(nn.Module):
    """
    一种高效的Mish激活函数  不采用自动求导(自己写前向传播和反向传播) 更高效
    """
    class F(torch.autograd.Function):
        @staticmethod
        def forward(ctx, x):
            # 前向传播
            # save_for_backward函数可以将对象保存起来，用于后续的backward函数
            # 会保留此input的全部信息，并提供避免in_place操作导致的input在backward中被修改的情况
            ctx.save_for_backward(x)
            return x.mul(torch.tanh(F.softplus(x)))  # x * tanh(ln(1 + exp(x)))

        @staticmethod
        def backward(ctx, grad_output):
            # 反向传播
            x = ctx.saved_tensors[0]
            sx = torch.sigmoid(x)
            fx = F.softplus(x).tanh()
            return grad_output * (fx + x * sx * (1 - fx * fx))

    def forward(self, x):
        return self.F.apply(x)

class FReLU(nn.Module):
    """
    FReLU https://arxiv.org/abs/2007.11824
    """
    def __init__(self, c1, k=3):  # ch_in, kernel
        super().__init__()
        # 定义漏斗条件T(x)  参数池窗口（Parametric Pooling Window ）来创建空间依赖
        # nn.Con2d(in_channels, out_channels, kernel_size, stride, padding, dilation=1, bias=True)
        # 使用 深度可分离卷积 DepthWise Separable Conv + BN 实现T(x)
        self.conv = nn.Conv2d(c1, c1, k, 1, 1, groups=c1, bias=False)
        self.bn = nn.BatchNorm2d(c1)

    def forward(self, x):
        # f(x)=max(x, T(x))
        return torch.max(x, self.bn(self.conv(x)))

class AconC(nn.Module):
    """
    ACON https://arxiv.org/pdf/2009.04759.pdf
    ACON activation (activate or not).
    AconC: (p1*x-p2*x) * sigmoid(beta*(p1*x-p2*x)) + p2*x, beta is a learnable parameter
    according to "Activate or Not: Learning Customized Activation" <https://arxiv.org/pdf/2009.04759.pdf>.
    """

    def __init__(self, c1):
        super().__init__()
        self.p1 = nn.Parameter(torch.randn(1, c1, 1, 1))
        self.p2 = nn.Parameter(torch.randn(1, c1, 1, 1))
        self.beta = nn.Parameter(torch.ones(1, c1, 1, 1))

    def forward(self, x):
        dpx = (self.p1 - self.p2) * x
        return dpx * torch.sigmoid(self.beta * dpx) + self.p2 * x

class MetaAconC(nn.Module):
    r""" ACON activation (activate or not).
    MetaAconC: (p1*x-p2*x) * sigmoid(beta*(p1*x-p2*x)) + p2*x, beta is generated by a small network
    according to "Activate or Not: Learning Customized Activation" <https://arxiv.org/pdf/2009.04759.pdf>.
    """

    def __init__(self, c1, k=1, s=1, r=16):  # ch_in, kernel, stride, r
        super().__init__()
        c2 = max(r, c1 // r)
        self.p1 = nn.Parameter(torch.randn(1, c1, 1, 1))
        self.p2 = nn.Parameter(torch.randn(1, c1, 1, 1))
        self.fc1 = nn.Conv2d(c1, c2, k, s, bias=True)
        self.fc2 = nn.Conv2d(c2, c1, k, s, bias=True)
        # self.bn1 = nn.BatchNorm2d(c2)
        # self.bn2 = nn.BatchNorm2d(c1)

    def forward(self, x):
        y = x.mean(dim=2, keepdims=True).mean(dim=3, keepdims=True)
        # batch-size 1 bug/instabilities https://github.com/ultralytics/yolov5/issues/2891
        # beta = torch.sigmoid(self.bn2(self.fc2(self.bn1(self.fc1(y)))))  # bug/unstable
        beta = torch.sigmoid(self.fc2(self.fc1(y)))  # bug patch BN layers removed
        dpx = (self.p1 - self.p2) * x
        return dpx * torch.sigmoid(beta * dpx) + self.p2 * x

class DyReLU(nn.Module):
    def __init__(self, channels, reduction=4, k=2, conv_type='2d'):
        super(DyReLU, self).__init__()
        self.channels = channels
        self.k = k
        self.conv_type = conv_type
        assert self.conv_type in ['1d', '2d']

        self.fc1 = nn.Linear(channels, channels // reduction)
        self.relu = nn.ReLU(inplace=True)
        self.fc2 = nn.Linear(channels // reduction, 2*k)
        self.sigmoid = nn.Sigmoid()

        self.register_buffer('lambdas', torch.Tensor([1.]*k + [0.5]*k).float())
        self.register_buffer('init_v', torch.Tensor([1.] + [0.]*(2*k - 1)).float())

    def get_relu_coefs(self, x):
        theta = torch.mean(x, dim=-1)
        if self.conv_type == '2d':
            theta = torch.mean(theta, dim=-1)
        theta = self.fc1(theta)
        theta = self.relu(theta)
        theta = self.fc2(theta)
        theta = 2 * self.sigmoid(theta) - 1
        return theta

    def forward(self, x):
        raise NotImplementedError

class DyReLUA(DyReLU):
    """
    调用: self.relu = DyReLUB(64, conv_type='2d')   64=本层channels
    """
    def __init__(self, channels, reduction=4, k=2, conv_type='2d'):
        super(DyReLUA, self).__init__(channels, reduction, k, conv_type)
        self.fc2 = nn.Linear(channels // reduction, 2*k)

    def forward(self, x):
        assert x.shape[1] == self.channels
        theta = self.get_relu_coefs(x)  # 这里是执行到normalize
        relu_coefs = theta.view(-1, 2*self.k) * self.lambdas + self.init_v  # 这里是执行完 theta(x)

        # BxCxL -> LxCxBx1
        x_perm = x.transpose(0, -1).unsqueeze(-1)
        # a^k_c=relu_coefs[:, :self.k]    b^k_c=relu_coefs[:, self.k:]
        # a^k_c(x) * x_c + b^k_c(x)
        output = x_perm * relu_coefs[:, :self.k] + relu_coefs[:, self.k:]
        # LxCxBx2 -> BxCxL
        # y_c = max{a^k_c(x) * x_c + b^k_c(x)}
        result = torch.max(output, dim=-1)[0].transpose(0, -1)

        return result

class DyReLUB(DyReLU):
    def __init__(self, channels, reduction=4, k=2, conv_type='2d'):
        super(DyReLUB, self).__init__(channels, reduction, k, conv_type)
        self.fc2 = nn.Linear(channels // reduction, 2*k*channels)

    def forward(self, x):
        assert x.shape[1] == self.channels
        theta = self.get_relu_coefs(x)

        relu_coefs = theta.view(-1, self.channels, 2*self.k) * self.lambdas + self.init_v

        if self.conv_type == '1d':
            # BxCxL -> LxBxCx1
            x_perm = x.permute(2, 0, 1).unsqueeze(-1)
            output = x_perm * relu_coefs[:, :, :self.k] + relu_coefs[:, :, self.k:]
            # LxBxCx2 -> BxCxL
            result = torch.max(output, dim=-1)[0].permute(1, 2, 0)

        elif self.conv_type == '2d':
            # BxCxHxW -> HxWxBxCx1
            x_perm = x.permute(2, 3, 0, 1).unsqueeze(-1)
            output = x_perm * relu_coefs[:, :, :self.k] + relu_coefs[:, :, self.k:]
            # HxWxBxCx2 -> BxCxHxW
            result = torch.max(output, dim=-1)[0].permute(2, 3, 0, 1)

        return result

