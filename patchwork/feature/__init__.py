from patchwork.feature._contextencoder import build_inpainting_network, ContextEncoderTrainer
from patchwork.feature._models import BNAlexNetFCN
from patchwork.feature._deepcluster import DeepClusterTrainer
from patchwork.feature._iic import InvariantInformationClusteringTrainer
from patchwork.feature._moco import MomentumContrastTrainer
from patchwork.feature._simclr import SimCLRTrainer