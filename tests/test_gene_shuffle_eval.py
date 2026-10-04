import numpy as np
import pandas as pd
import torch

from longevity.gene_shuffle_eval import predict, shuffle_batch


def test_shuffle_preserves_all_special_positions_and_is_batch_independent():
    inputs = torch.tensor([[1, 10, 11, 12, 2, 3, 3, 3], [1, 3, 10, 2, 11, 12, 13, 2]])
    mask = torch.tensor([[1, 1, 1, 1, 1, 0, 0, 0], [1, 1, 1, 1, 1, 1, 1, 1]])
    original = inputs.clone()
    shuffled = shuffle_batch(inputs, mask, ["a", "b"])
    special = (inputs == 1) | (inputs == 2) | (inputs == 3) | (mask == 0)
    assert torch.equal(shuffled[special], inputs[special])
    assert torch.equal(shuffled.sort(dim=1).values, inputs.sort(dim=1).values)
    assert torch.equal(inputs, original)
    assert torch.equal(shuffle_batch(inputs.flip(0), mask.flip(0), ["b", "a"]).flip(0), shuffled)
    assert not torch.equal(inputs, shuffled)


def test_shuffling_occurs_after_eval_crop_and_keeps_padding():
    class Model(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.inputs = []

        def forward(self, inputs, mask):
            self.inputs.append((inputs.clone(), mask.clone()))
            return (inputs * mask).float().sum(dim=1)

    model = Model()
    frame = pd.DataFrame(
        {
            "assembly_accession": ["a", "b"],
            "entrez_id": [1, 2],
            "source_row": [0, 1],
            "input_ids": [np.arange(10, 1110), np.arange(10, 20)],
            "n_tokens": [1100, 10],
            "ncbi_taxid": [1, 2],
            "scientific_name": ["a", "b"],
            "class": [None, None],
            "hgnc_symbol": ["a", "b"],
            "log10_longevity": [1.0, 2.0],
        }
    )
    cfg = {"eval_tokens_per_batch": 4096, "max_len": 1024, "mu": 0.0, "sd": 1.0}
    predict(model, frame, cfg, torch.device("cpu"), False, 0)
    predict(model, frame, cfg, torch.device("cpu"), True, 0)
    (intact, mask1), (shuffled, mask2) = model.inputs
    assert torch.equal(mask1, mask2)
    assert torch.equal(intact.sort(dim=1).values, shuffled.sort(dim=1).values)
    assert (intact < 1032).all()  # discarded suffix never enters the shuffle pool
    assert torch.equal(intact[mask1 == 0], shuffled[mask1 == 0])
