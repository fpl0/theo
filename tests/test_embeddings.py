import re

from theo.embeddings import _map_hf_key


def test_embedding_key_maps_word_embeddings() -> None:
    assert _map_hf_key("bert.embeddings.word_embeddings.weight") == "word_emb.weight"


def test_embedding_key_maps_position_embeddings() -> None:
    assert _map_hf_key("bert.embeddings.position_embeddings.weight") == "pos_emb.weight"


def test_embedding_key_maps_layer_norm() -> None:
    assert _map_hf_key("bert.embeddings.LayerNorm.weight") == "norm.weight"
    assert _map_hf_key("bert.embeddings.LayerNorm.bias") == "norm.bias"


def test_embedding_key_maps_encoder_attention() -> None:
    assert _map_hf_key("bert.encoder.layer.0.attention.self.query.weight") == (
        "layers.0.attn.query_proj.weight"
    )
    assert _map_hf_key("bert.encoder.layer.5.attention.self.key.bias") == (
        "layers.5.attn.key_proj.bias"
    )
    assert _map_hf_key("bert.encoder.layer.11.attention.output.dense.weight") == (
        "layers.11.attn.out_proj.weight"
    )


def test_embedding_key_maps_encoder_ff() -> None:
    assert _map_hf_key("bert.encoder.layer.0.intermediate.dense.weight") == (
        "layers.0.ff.layers.0.weight"
    )
    assert _map_hf_key("bert.encoder.layer.0.output.dense.weight") == (
        "layers.0.ff.layers.2.weight"
    )


def test_embedding_key_maps_encoder_layer_norms() -> None:
    assert _map_hf_key("bert.encoder.layer.0.attention.output.LayerNorm.weight") == (
        "layers.0.ln1.weight"
    )
    assert _map_hf_key("bert.encoder.layer.0.output.LayerNorm.bias") == "layers.0.ln2.bias"


def test_embedding_key_skips_pooler() -> None:
    assert _map_hf_key("bert.pooler.dense.weight") is None
    assert _map_hf_key("cls.predictions.bias") is None


def test_layer_regex_matches_expected_format() -> None:
    pattern = re.compile(r"bert\.encoder\.layer\.(\d+)\.(.+)")
    m = pattern.match("bert.encoder.layer.11.output.dense.weight")
    assert m is not None
    assert m.group(1) == "11"
    assert m.group(2) == "output.dense.weight"
