from typing import Generator

import pytest

from gdsclient.graph.graph_object import Graph
from gdsclient.graph_data_science import GraphDataScience
from gdsclient.model.trained_model import GraphSageModel
from gdsclient.query_runner.neo4j_query_runner import Neo4jQueryRunner

MODEL_NAME = "gs"


@pytest.fixture
def G(runner: Neo4jQueryRunner, gds: GraphDataScience) -> Generator[Graph, None, None]:
    runner.run_query(
        """
        CREATE
        (a: Node {x: 3}),
        (b: Node {x: 5}),
        (c: Node {x: 7}),
        (a)-[:REL]->(b),
        (a)-[:REL]->(c),
        (b)-[:REL]->(c),
        (b)-[:REL]->(a),
        (c)-[:REL]->(a),
        (c)-[:REL]->(b)
        """
    )
    G = gds.graph.project("g", "*", "*", nodeProperties=["x"])

    yield G

    runner.run_query("MATCH (n) DETACH DELETE n")
    G.drop()


@pytest.fixture
def model(gds: GraphDataScience, G: Graph) -> Generator[GraphSageModel, None, None]:
    model = gds.beta.graphSage.train(
        G, modelName="m", featureProperties=["x"], embeddingDimension=20
    )

    yield model

    model.drop()


def test_graphsage_train(model: GraphSageModel) -> None:
    assert model.name() == "m"
    assert model.exists()


def test_graphsage_write(
    G: Graph, model: GraphSageModel, runner: Neo4jQueryRunner
) -> None:
    model.predict_write(G, writeProperty="gs")

    result = runner.run_query("MATCH (n:Node) RETURN size(n.gs) AS embeddingDim")
    assert len(result) == G.node_count()
    assert result[0]["embeddingDim"] == 20


def test_graphsage_stream(
    G: Graph, model: GraphSageModel, runner: Neo4jQueryRunner
) -> None:
    stream = model.predict_stream(G)

    assert len(stream) == G.node_count()
    assert len(stream[0]["embedding"]) == 20
