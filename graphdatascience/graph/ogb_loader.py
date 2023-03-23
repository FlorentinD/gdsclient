import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Protocol, Tuple, TypedDict, Union
from warnings import warn

import numpy as np
import numpy.typing as npt
import pandas as pd

from ..error.client_only_endpoint import client_only_endpoint
from ..error.illegal_attr_checker import IllegalAttrChecker
from ..error.uncallable_namespace import UncallableNamespace
from ..graph.graph_object import Graph
from ..query_runner.query_runner import QueryRunner
from ..server_version.compatible_with import compatible_with
from ..server_version.server_version import ServerVersion


class _HomogeneousOGBGraphBase(TypedDict):
    edge_index: npt.NDArray[np.int64]
    num_nodes: int


class HomogeneousOGBGraph(_HomogeneousOGBGraphBase, total=False):
    edge_feat: Optional[npt.NDArray[np.float64]]
    node_feat: Optional[npt.NDArray[np.float64]]


class _HeterogeneousOGBGraphBase(TypedDict):
    edge_index_dict: Dict[Tuple[str, str, str], npt.NDArray[np.int64]]
    num_nodes_dict: Dict[str, int]


class HeterogeneousOGBGraph(_HeterogeneousOGBGraphBase, total=False):
    edge_feat_dict: Dict[Tuple[str, str, str], npt.NDArray[np.float64]]
    node_feat_dict: Dict[str, npt.NDArray[np.float64]]


class HomogeneousOGBNDataset(Protocol):
    graph: HomogeneousOGBGraph
    # `labels` here refers to class labels, not node labels in the Neo4j sense
    # The representation is a node_count x 1 shaped matrix
    labels: npt.NDArray[np.int64]

    @abstractmethod
    def get_idx_split(self) -> Dict[str, npt.NDArray[np.int64]]:
        pass


class HomogeneousOGBLDataset(Protocol):
    graph: HomogeneousOGBGraph

    @abstractmethod
    def get_edge_split(self) -> Dict[str, Dict[str, npt.NDArray[np.int64]]]:
        pass


class HeterogeneousOGBNDataset(Protocol):
    graph: HeterogeneousOGBGraph
    # `labels` here refers to class labels, not node labels in the Neo4j sense
    # The representation is a node_count x 1 shaped matrix
    labels: Dict[str, npt.NDArray[np.int64]]

    @abstractmethod
    def get_idx_split(self) -> Dict[str, Dict[str, npt.NDArray[np.int64]]]:
        pass


class HeterogeneousOGBLDataset(Protocol):
    graph: HeterogeneousOGBGraph

    @abstractmethod
    def get_edge_split(self) -> Dict[str, Dict[str, Any]]:
        pass


class OGBLoader(UncallableNamespace, IllegalAttrChecker, ABC):
    def __init__(self, query_runner: QueryRunner, namespace: str, server_version: ServerVersion):
        self._query_runner = query_runner
        self._namespace = namespace
        self._server_version = server_version
        self._logger = logging.getLogger()

    def _load(self, graph_name: str, nodes: List[pd.DataFrame], rels: List[pd.DataFrame], concurrency: int) -> Graph:
        constructor = self._query_runner.create_graph_constructor(graph_name, concurrency, [])
        constructor.run(nodes, rels)

        return Graph(graph_name, self._query_runner, self._server_version)


class OGBNLoader(OGBLoader):
    def _parse_homogeneous(self, dataset: HomogeneousOGBNDataset) -> Tuple[List[pd.DataFrame], List[pd.DataFrame]]:
        graph: HomogeneousOGBGraph = dataset.graph

        if "edge_feat" in graph and graph["edge_feat"] is not None:
            warn("Edge features are not supported and will not be loaded")

        self._logger.info("Preparing node data for transfer to server...")

        node_count = graph["num_nodes"]

        node_dict: Dict[str, List[Any]] = {
            "nodeId": list(range(node_count)),
        }
        if "node_feat" in graph and graph["node_feat"] is not None:
            node_dict["features"] = graph["node_feat"].tolist()

        node_dict["classLabel"] = [cl[0] for cl in dataset.labels]

        split = dataset.get_idx_split()
        node_labels = ["Train" for _ in range(node_count)]
        for node_id in split["valid"]:
            node_labels[node_id] = "Valid"
        for node_id in split["test"]:
            node_labels[node_id] = "Test"
        node_dict["labels"] = node_labels

        nodes = pd.DataFrame(node_dict)

        self._logger.info("Preparing relationship data for transfer to server...")

        relationships = pd.DataFrame(
            {
                "sourceNodeId": graph["edge_index"][0],
                "targetNodeId": graph["edge_index"][1],
                "relationshipType": "R",
            }
        )

        return [nodes], [relationships]

    def _parse_heterogeneous(self, dataset: HeterogeneousOGBNDataset) -> Tuple[List[pd.DataFrame], List[pd.DataFrame]]:
        graph: HeterogeneousOGBGraph = dataset.graph
        class_labels = dataset.labels

        if "edge_feat_dict" in graph and graph["edge_feat_dict"] is not None:
            warn("Edge features are not supported and will not be loaded")

        self._logger.info("Preparing node data for transfer to server...")

        node_features = {}
        if "node_feat_dict" in graph and graph["node_feat_dict"] is not None:
            node_features = graph["node_feat_dict"]

        split = dataset.get_idx_split()
        node_id_offsets = {}
        current_offset = 0
        nodes = []

        for node_label, node_count in graph["num_nodes_dict"].items():
            node_labels: Union[str, List[List[str]]] = node_label
            if node_label in split["train"]:
                node_labels = [[node_label, "Train"] for _ in range(node_count)]
                for node_id in split["valid"][node_label]:
                    node_labels[node_id] = [node_label, "Valid"]
                for node_id in split["test"][node_label]:
                    node_labels[node_id] = [node_label, "Test"]

            node_dict = {
                "nodeId": range(current_offset, current_offset + node_count),
                "labels": node_labels,
            }

            if node_label in node_features:
                node_dict["features"] = node_features[node_label].tolist()

            if node_label in class_labels:
                node_dict["classLabel"] = [cl[0] for cl in class_labels[node_label]]

            node_id_offsets[node_label] = current_offset
            current_offset += node_count

            nodes.append(pd.DataFrame(node_dict))

        self._logger.info("Preparing relationship data for transfer to server...")

        rels = []
        for rel_triple, edge_index in graph["edge_index_dict"].items():
            _, rel_type, _ = rel_triple

            rel_dict = {
                "sourceNodeId": edge_index[0],
                "targetNodeId": edge_index[1],
                "relationshipType": rel_type,
            }

            rels.append(pd.DataFrame(rel_dict))

        return nodes, rels

    @client_only_endpoint("gds.graph.ogbn")
    @compatible_with("load", min_inclusive=ServerVersion(2, 1, 0))
    def load(
        self,
        dataset_name: str,
        dataset_root_path: str = "dataset",
        graph_name: Optional[str] = None,
        concurrency: int = 4,
    ) -> Graph:
        try:
            from ogb.nodeproppred import NodePropPredDataset
        except ModuleNotFoundError:
            raise ModuleNotFoundError(
                "This feature requires OGB support. "
                "You can add OGB support by running `pip install graphdatascience[ogb]`"
            )

        dataset = NodePropPredDataset(name=dataset_name, root=dataset_root_path)

        if dataset.is_hetero:
            nodes, rels = self._parse_heterogeneous(dataset)
        else:
            nodes, rels = self._parse_homogeneous(dataset)

        if not graph_name:
            graph_name = dataset_name

        return self._load(graph_name, nodes, rels, concurrency)


class OGBLLoader(OGBLoader):
    def _parse_homogeneous(self, dataset: HomogeneousOGBLDataset) -> Tuple[List[pd.DataFrame], List[pd.DataFrame]]:
        graph: HomogeneousOGBGraph = dataset.graph

        if "edge_feat" in graph and graph["edge_feat"] is not None:
            warn("Edge features are not supported and will not be loaded")

        self._logger.info("Preparing node data for transfer to server...")

        node_dict = {
            "nodeId": range(graph["num_nodes"]),
            "labels": "N",
        }
        if "node_feat" in graph and graph["node_feat"] is not None:
            node_dict["features"] = graph["node_feat"].tolist()
        nodes = pd.DataFrame(node_dict)

        self._logger.info("Preparing relationship data for transfer to server...")

        source_ids = []
        target_ids = []
        rel_types = []
        split = dataset.get_edge_split()

        for set_type, edges in split.items():
            if "edge" in edges:
                rel_type = f"{set_type.upper()}_POS"
                for source_id, target_id in edges["edge"]:
                    source_ids.append(source_id)
                    target_ids.append(target_id)
                    rel_types.append(rel_type)
            if "edge_neg" in edges:
                rel_type = f"{set_type.upper()}_NEG"
                for source_id, target_id in edges["edge_neg"]:
                    source_ids.append(source_id)
                    target_ids.append(target_id)
                    rel_types.append(rel_type)

        relationships = pd.DataFrame(
            {"sourceNodeId": source_ids, "targetNodeId": target_ids, "relationshipType": rel_types}
        )

        return [nodes], [relationships]

    def _parse_heterogeneous(self, dataset: HeterogeneousOGBLDataset) -> Tuple[List[pd.DataFrame], List[pd.DataFrame]]:
        graph: HeterogeneousOGBGraph = dataset.graph

        if "edge_feat_dict" in graph and graph["edge_feat_dict"] is not None:
            warn("Edge features are not supported and will not be loaded")

        self._logger.info("Preparing node data for transfer to server...")

        node_features = {}
        if "node_feat_dict" in graph and graph["node_feat_dict"] is not None:
            node_features = graph["node_feat_dict"]

        node_id_offsets = {}
        current_offset = 0
        nodes = []
        for node_label, node_count in graph["num_nodes_dict"].items():
            node_dict = {
                "nodeId": range(current_offset, current_offset + node_count),
                "labels": node_label,
            }

            if node_label in node_features:
                node_dict["features"] = node_features[node_label].tolist()

            node_id_offsets[node_label] = current_offset
            current_offset += node_count

            nodes.append(pd.DataFrame(node_dict))

        self._logger.info("Preparing relationship data for transfer to server...")

        split = dataset.get_edge_split()
        available_rel_types = list(graph["edge_index_dict"].keys())
        rels = []
        for set_type, edges in split.items():
            source_labels = edges["head_type"]
            target_labels = edges["tail_type"]
            source_ids = edges["head"]
            target_ids = edges["tail"]
            class_labels = edges["relation"]
            rel_types = []

            for i, class_label in enumerate(class_labels):
                source_label, edge_type, target_label = available_rel_types[class_label]
                assert source_labels[i] == source_label
                assert target_labels[i] == target_label

                rel_types.append(f"{edge_type}_{set_type.upper()}")

            rels.append(
                pd.DataFrame(
                    {
                        "sourceNodeId": source_ids,
                        "targetNodeId": target_ids,
                        "relationshipType": rel_types,
                        "classLabel": class_labels,
                    }
                )
            )

        return nodes, rels

    @client_only_endpoint("gds.graph.ogbl")
    @compatible_with("load", min_inclusive=ServerVersion(2, 1, 0))
    def load(
        self,
        dataset_name: str,
        dataset_root_path: str = "dataset",
        graph_name: Optional[str] = None,
        concurrency: int = 4,
    ) -> Graph:
        try:
            from ogb.linkproppred import LinkPropPredDataset
        except ModuleNotFoundError:
            raise ModuleNotFoundError(
                "This feature requires OGB support. "
                "You can add OGB support by running `pip install graphdatascience[ogb]`"
            )

        dataset = LinkPropPredDataset(name=dataset_name, root=dataset_root_path)

        if dataset.is_hetero:
            nodes, rels = self._parse_heterogeneous(dataset)
        else:
            nodes, rels = self._parse_homogeneous(dataset)

        if not graph_name:
            graph_name = dataset_name

        return self._load(graph_name, nodes, rels, concurrency)
