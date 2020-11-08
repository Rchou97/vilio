#!/bin/bash

# Allows for not having to copy the models to vilio/data
loadfin=${1:-./data/LASTtrain.pth}
loadfin2=${2:-./data/LASTtraindev.pth}

# 72 Feats, Seed 98
cp ./data/hm_vgattr7272.tsv ./data/HM_img.tsv

python hm.py --seed 98 --model D \
--test dev_seen --lr 1e-5 --batchSize 8 --tr bert-base-uncased --epochs 5 --tsv \
--num_features 72 --loadfin $loadfin --exp D72 --subtest

python hm.py --seed 98 --model D \
--test dev_seen --lr 1e-5 --batchSize 8 --tr bert-base-uncased --epochs 5 --tsv \
--num_features 72 --loadfin $loadfin --exp D72 --subtest --combine