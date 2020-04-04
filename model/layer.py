import torch
from torch import nn
import dgl
import dgl.function as fn
import numpy as np


class CompGCNCov(nn.Module):
    def __init__(self, in_channels, out_channels, act=lambda x: x, bias=True, drop_rate=0., opn='mult'):
        super(CompGCNCov, self).__init__()
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.act = act  # activation function
        self.device = None
        self.rel = None
        self.opn = opn

        # relation-type specific parameter
        self.w = self.get_param([3, in_channels, out_channels])  # weight matrix for 3 directions (int, out, self-loop)
        self.w_rel = self.get_param([in_channels, out_channels])  # transform embedding of relations to next layer
        self.loop_rel = self.get_param([1, in_channels])  # self-loop embedding

        self.drop = nn.Dropout(drop_rate)
        self.bn = torch.nn.BatchNorm1d(out_channels)
        self.bias = nn.Parameter(torch.zeros(out_channels)) if bias else None

    def get_param(self, shape):
        param = nn.Parameter(torch.Tensor(*shape))
        nn.init.xavier_normal_(param, gain=nn.init.calculate_gain('relu'))
        return param

    def message_func(self, edges: dgl.EdgeBatch):
        # h = edges.src['h']  # [E, in_channel]
        edge_type = edges.data['type']  # [E, 1]
        edge_dir = edges.data['dir']  # [E, 1]
        # edge_data = self.rel[edge_type]  # [E, D]
        edge_data = self.comp(edges.src['h'], self.rel[edge_type])  # [E, in_channel]
        msg = torch.bmm(edge_data.unsqueeze(1),
                        self.w[edge_dir.squeeze()]).squeeze()  # [E, 1, in_c] @ [E, in_c, out_c]
        msg = msg * edges.data['norm'].reshape(-1, 1)  # [E, D] * [E, 1]
        return {'msg': msg}

    def reduce_func(self, nodes: dgl.NodeBatch):
        return {'h': self.drop(nodes.data['h']) / 3}

    def comp(self, h, edge_data):
        if self.opn == 'mult':
            return h * edge_data
        elif self.opn == 'sub':
            return h - edge_data
        else:
            raise NotImplementedError

    def compute_norm(self, g: dgl.DGLGraph):
        g.local_var()
        in_deg = g.in_degrees(range(g.number_of_nodes())).float().numpy()
        norm = in_deg ** -0.5
        norm[np.isinf(norm)] = 0
        g.ndata['xxx'] = norm
        g.apply_edges(lambda edges: {'xxx': edges.dst['xxx'] * edges.src['xxx']})
        norm = g.edata['xxx']
        return norm.squeeze()

    def forward(self, g: dgl.DGLGraph, x, rel_repr, edge_type, edge_dir):
        """
        :param g: dgl Graph, a graph without self-loop
        :param x: input node features, [V, in_channel]
        :param rel_repr: input relation features: [num_rel*2, in_channel]
        :param edge_type: edge type, [E]
        :param edge_dir: edge direction, [E]
        :param norm: edge norm, [E, 1]
        :return: x: output node features: [V, out_channel]
                 rel: output relation features: [num_rel*2, out_channel]
        """
        self.device = x.device
        g = g.local_var()
        g.ndata['h'] = x
        norm = self.compute_norm(g)
        g.add_edges(range(g.number_of_nodes()), range(g.number_of_nodes()))  # self-loop
        # g.edata['type'] = edge_type
        self_loop_edge_type = torch.tensor([rel_repr.shape[0]] * g.number_of_nodes(), device=self.device)
        self_loop_edge_dir = torch.tensor([2] * g.number_of_nodes(), device=self.device)
        self_loop_edge_norm = torch.ones_like(self_loop_edge_dir, device=self.device, dtype=torch.float32)
        g.edata['type'] = torch.cat([edge_type, self_loop_edge_type])
        g.edata['dir'] = torch.cat([edge_dir, self_loop_edge_dir])
        g.edata['norm'] = torch.cat([norm, self_loop_edge_norm])
        # if norm is not None:
        #     g.edata['norm'] = norm
        self.rel = torch.cat([rel_repr, self.loop_rel], dim=0)
        g.update_all(self.message_func, fn.sum(msg='msg', out='h'), self.reduce_func)
        x = g.ndata['h']
        if self.bias is not None:
            x = x + self.bias
        x = self.bn(x)
        return self.act(x), torch.matmul(rel, self.w_rel)[:, -1]


if __name__ == '__main__':
    compgcn = CompGCNCov(in_channels=10, out_channels=5)
    src, tgt = [0, 1, 0, 3, 2], [1, 3, 3, 4, 4]
    g = dgl.DGLGraph()
    g.add_nodes(5)
    g.add_edges(src, tgt)  # src -> tgt
    g.add_edges(tgt, src)  # tgt -> src
    # g.add_edges(range(g.number_of_nodes()), range(g.number_of_nodes()))  # self-loop
    #
    # import numpy as np
    #
    # in_deg = g.in_degrees(range(g.number_of_nodes())).float().numpy()
    # norm = in_deg ** -0.5
    # norm[np.isinf(norm)] = 0
    # g.ndata['xxx'] = norm
    # g.apply_edges(lambda edges: {'xxx': edges.dst['xxx'] * edges.src['xxx']})
    # norm = g.edata['xxx']
    # print(norm)

    edge_dir = torch.tensor([0] * len(src) + [1] * len(tgt))
    edge_type = torch.tensor([0, 0, 0, 1, 1] + [2, 2, 2, 3, 3])
    x = torch.randn([5, 10])
    rel = torch.randn([4, 10])  # 2*2+1
    x, rel = compgcn(g, x, rel, edge_type, edge_dir)
    print(x.shape, rel.shape)
