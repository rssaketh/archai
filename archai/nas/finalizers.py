# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

from typing import List, Tuple, Optional, Iterator
from overrides import EnforceOverrides

from torch import nn

from archai.nas.model import Model
from archai.nas.cell import Cell
from archai.nas.model_desc import CellDesc, ModelDesc, NodeDesc, EdgeDesc

class Finalizers(EnforceOverrides):
    """Provides base algorithms for finalizing model, cell and edge which can be overriden

    For op-level finalize, just put logic in op's finalize.

    For model/cell/edge level finalize, you can override the methods in this class to customize the behavior. To override any of these methods, simply create new class in your algos folder, for example, diversity/diversity_finalizers.py. In this file create class that derives from Finalizers. Then in your algos exp_runner.py, return instance of that class in its finalizers() method.
    """

    def finalize_model(self, model:Model, to_cpu=True, restore_device=True)->ModelDesc:
        # move model to CPU before finalize because each op will serialize
        # its parameters and we don't want copy of these parameters hanging on GPU
        original = model.device_type()
        if to_cpu:
            model.cpu()

        # finalize will create copy of state and this can overflow GPU RAM
        assert model.device_type() == 'cpu'

        cell_descs = self.finalize_cells(model)

        if restore_device:
            model.to(original, non_blocking=True)

        return ModelDesc(stem0_op=model.stem0_op.finalize()[0],
                         stem1_op=model.stem1_op.finalize()[0],
                         pool_op=model.pool_op.finalize()[0],
                         ds_ch=model.desc.ds_ch,
                         n_classes=model.desc.n_classes,
                         cell_descs=cell_descs,
                         aux_tower_descs=model.desc.aux_tower_descs,
                         logits_op=model.logits_op.finalize()[0],
                         params=model.desc.params)

    def finalize_cells(self, model:Model)->List[CellDesc]:
        return [self.finalize_cell(cell) for cell in model.cells]

    def finalize_cell(self, cell:Cell, *args, **kwargs)->CellDesc:
        # first finalize each node, we will need to recreate node desc with final version
        node_descs:List[NodeDesc] = []
        for node in cell.dag:
            node_desc = self.finalize_node(node,  cell.desc.max_final_edges)
            node_descs.append(node_desc)

        finalized = CellDesc(
            cell_type=cell.desc.cell_type,
            id = cell.desc.id,
            nodes = node_descs,
            s0_op=cell.s0_op.finalize()[0],
            s1_op=cell.s1_op.finalize()[0],
            template_cell = cell.desc.template_cell,
            max_final_edges=cell.desc.max_final_edges,
            node_ch_out=cell.desc.node_ch_out,
            post_op=cell.post_op.finalize()[0]
        )
        return finalized

    def finalize_node(self, node:nn.ModuleList, max_final_edges:int, *args, **kwargs)->NodeDesc:
        # get edge ranks, if rank is None it is deemed as required
        pre_selected, edge_desc_ranks = self.get_edge_ranks(node)
        ranked_selected = self.select_edges(edge_desc_ranks, max_final_edges)
        selected_edges = pre_selected + ranked_selected
        return NodeDesc(selected_edges)

    def select_edges(self, edge_desc_ranks:List[Tuple[EdgeDesc, float]],
                           max_final_edges:int)->List[EdgeDesc]:
        if len(edge_desc_ranks) > max_final_edges:
            # sort by rank and pick bottom
            edge_desc_ranks.sort(key=lambda d:d[1], reverse=True)
            edge_desc_ranks = edge_desc_ranks[:max_final_edges]
        return [edr[0] for edr in edge_desc_ranks]

    def get_edge_ranks(self, node:nn.ModuleList)\
            ->Tuple[List[EdgeDesc], List[Tuple[EdgeDesc, float]]]:
        selected_edges, edge_desc_ranks = [], []
        for edge in node:
            edge_desc, rank = self.finalize_edge(edge)
            # if rank is None then it is required rank
            if rank is None:
                selected_edges.append(edge_desc) # required edge
            else: # optional edge
                edge_desc_ranks.append((edge_desc, rank))
        return selected_edges, edge_desc_ranks

    def finalize_edge(self, edge)->Tuple[EdgeDesc, Optional[float]]:
        op_desc, rank = edge._op.finalize()
        return (EdgeDesc(op_desc, edge.input_ids), rank)