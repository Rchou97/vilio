import collections
import os
import random

from tqdm import tqdm
import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader

from param import args

if args.tsv:
    from pretrain_data_tsv import InputExample, LXMERTDataset, LXMERTTorchDataset
else:
    from pretrain_data import InputExample, LXMERTDataset, LXMERTTorchDataset 

from transformers import AutoTokenizer
from transformers.optimization import AdamW, get_linear_schedule_with_warmup, get_cosine_schedule_with_warmup
from modeling_bertU import BertUPretraining

from torch.nn.utils.rnn import pad_sequence

DataTuple = collections.namedtuple("DataTuple", 'dataset torchdset loader evaluator')

def get_tuple(splits: str, bs: int, shuffle=False, drop_last=False, topk=-1) -> DataTuple:
    # Decide which QA datasets would be used in pre-training.
    # Options: vqa, gqa, visual7w
    # Note: visual7w is a part of vgqa, we take the name here.
    qa_sets = args.qa_sets
    if qa_sets is not None:
        qa_sets = set(qa_set.lower().strip() for qa_set in qa_sets.split(","))
    
    print(splits)

    # Build dataset, data loader, and evaluator.
    dset = LXMERTDataset(splits)
    tset = LXMERTTorchDataset(splits) # Remove topk
    data_loader = DataLoader(
        tset, batch_size=bs,
        shuffle=shuffle, num_workers=args.num_workers,
        collate_fn=lambda x: x,
        drop_last=drop_last, pin_memory=True
    )
    #evaluator = LXMERTEvaluator(dset)
    evaluator = None
    print()

    return DataTuple(dataset=dset, torchdset=tset, loader=data_loader, evaluator=evaluator)

train_tuple = get_tuple(args.train, args.batch_size, shuffle=True, drop_last=True)
valid_tuple = None

class InputFeatures(object):
    """A single set of features of data."""

    def __init__(self,
                 input_ids, input_mask, segment_ids, lm_label_ids,
                 visual_feats, obj_labels,
                 is_matched, ans):
        self.input_ids = input_ids
        self.input_mask = input_mask
        self.segment_ids = segment_ids
        self.lm_label_ids = lm_label_ids

        self.visual_feats = visual_feats
        self.obj_labels = obj_labels

        self.is_matched = is_matched

        self.ans = ans

def random_word(tokens, tokenizer):
    """
    Masking some random tokens for Language Model task with probabilities as in the original BERT paper.
    :param tokens: list of str, tokenized sentence.
    :param tokenizer: Tokenizer, object used for tokenization (we need it's vocab here)
    :return: (list of str, list of int), masked tokens and related labels for LM prediction
    """
    output_label = []

    for i, token in enumerate(tokens):

        prob = random.random()
        # mask token with probability
        ratio = args.word_mask_rate
        if prob < ratio:
            prob /= ratio

            # 80% randomly change token to mask token
            if prob < 0.8:
                tokens[i] = "[MASK]"

            # 10% randomly change token to random token
            elif prob < 0.9:
                tokens[i] = random.choice(list(tokenizer.vocab.items()))[0]

            # -> rest 10% randomly keep current token

            # append current token to output (we will predict these later)
            try:
                output_label.append(tokenizer.vocab[token])
            except KeyError:
                # For unknown words (should not occur with BPE vocab)
                output_label.append(tokenizer.vocab["[UNK]"])
        else:
            # no masking token (will be ignored by loss function later)
            output_label.append(-1)


    return tokens, output_label


def random_feat(feats):
    mask_feats = feats.copy() #clone() #copy()
    feat_mask = np.zeros(len(feats), dtype=np.float32)
    for i in range(len(feats)):
        prob = random.random()
        # mask token with probability
        if prob < args.obj_mask_rate:
            prob /= args.obj_mask_rate

            # 80% randomly change token to zero feat
            if prob < 0.8:
                mask_feats[i, :] = 0.

            # 10% randomly change token to random feat
            elif prob < 0.9:
                mask_feats[i, :] = train_tuple.torchdset.random_feat()
            # -> rest 10% randomly keep current feat

            # Need to predict this feat
            feat_mask[i] = 1.

    return mask_feats, feat_mask


def convert_example_to_features(example: InputExample, max_seq_length, tokenizer)->InputFeatures:
    """
    Convert a raw sample (pair of sentences as tokenized strings) into a proper training sample with
    IDs, LM labels, input_mask, CLS and SEP tokens etc.
    :param example: InputExample, containing sentence input as strings and is_next label
    :param max_seq_length: int, maximum length of sequence.
    :param tokenizer: Tokenizer
    :return: InputFeatures, containing all inputs and labels of one sample as IDs (as used for model training)
    """
    tokens = tokenizer.tokenize(" ".join(str(example.sent).split()))

    # Account for [CLS] and [SEP] with "- 2"
    if len(tokens) > max_seq_length - 2:
        tokens = tokens[:(max_seq_length - 2)]

    # Get random words
    masked_tokens, masked_label = random_word(tokens, tokenizer)

    # concatenate lm labels and account for CLS, SEP
    masked_tokens = ['[CLS]'] + masked_tokens + ['[SEP]']
    input_ids = tokenizer.convert_tokens_to_ids(masked_tokens)

    # Mask & Segment Word
    lm_label_ids = ([-1] + masked_label + [-1])
    input_mask = [1] * len(input_ids)
    segment_ids = [0] * len(input_ids)

    # Zero-pad up to the sequence length.
    while len(input_ids) < max_seq_length:
        input_ids.append(0)
        input_mask.append(0)
        segment_ids.append(0)
        lm_label_ids.append(-1)
    
    # As VisualBERT concats Text & Visual Input, lm label ids must be even longer!
    num_features = args.num_features  # 100 features for Hateful Memes!
    while len(lm_label_ids) < (max_seq_length + num_features):
        lm_label_ids.append(-1)

    assert len(input_ids) == max_seq_length
    assert len(input_mask) == max_seq_length
    assert len(segment_ids) == max_seq_length
    assert len(lm_label_ids) == max_seq_length + num_features

    feat, boxes = example.visual_feats
    obj_labels, obj_confs = example.obj_labels
    attr_labels, attr_confs = example.attr_labels

    # Mask Image Features:
    masked_feat, feat_mask = random_feat(feat)

    ans = -1

    features = InputFeatures(
        input_ids=input_ids,
        input_mask=input_mask,
        segment_ids=segment_ids,
        lm_label_ids=lm_label_ids,
        visual_feats=(masked_feat, boxes),
        obj_labels={
            'obj': (obj_labels, obj_confs),
            'attr': (attr_labels, attr_confs),
            'feat': (feat, feat_mask),
        },
        is_matched=example.is_matched,
        ans=ans,
    )
    return features


LOSSES_NAME = ('Mask_LM', 'Matched', 'Obj', 'Attr', 'Feat') #Removed , 'Matched', 'Obj', 'Feat' 'Attr', 'QA'

## I.e. : Mask_LM = Masking words; 
# Obj, Feat = Masking objs (ids), feats (pixels?), 
# Matched = Sen & Img belong together? 

class LXMERT:
    def __init__(self, max_seq_length):
        super().__init__()
        self.max_seq_length = max_seq_length

        self.tokenizer = AutoTokenizer.from_pretrained(args.tr, do_lower_case=True)

        # Build model
        self.model = BertUPretraining.from_pretrained(
            args.tr,
            visual_losses=args.visual_losses,
            task_matched=args.task_matched,
            task_obj_predict=args.task_obj_predict,
            task_mask_lm=args.task_mask_lm
        )
        
        # Weight initialization and loading
        if args.from_scratch:
            print("Train from Scratch: re-initialize all BERT weights.")
            self.model.apply(self.model.init_bert_weights)
        if args.load is not None:
            self.load(args.load)
        if args.load_lxmert is not None:
            # Load lxmert would not load the answer head.
            self.load_lxmert(args.load_lxmert)

        # GPU Options
        self.model = self.model.cuda()
        if args.multiGPU:
            self.model = nn.DataParallel(self.model)

    def pad_tensors(self, tensors, lens=None, pad=0):
        """Copied from UNITER Repo --- B x [T, ...]"""
        if lens is None:
            lens = [t.size(0) for t in tensors]
        max_len = max(lens)
        bs = len(tensors)
        hid = tensors[0].size(-1)
        dtype = tensors[0].dtype
        output = torch.zeros(bs, max_len, hid, dtype=dtype)
        if pad:
            output.data.fill_(pad)
        for i, (t, l) in enumerate(zip(tensors, lens)):
            output.data[i, :l, ...] = t.data
        return output
    
    def get_gather_index(self, txt_lens, num_bbs, batch_size, max_len, out_size):

        assert len(txt_lens) == len(num_bbs) == batch_size
        gather_index = torch.arange(0, out_size, dtype=torch.long,
                                    ).unsqueeze(0).repeat(batch_size, 1)


        for i, (tl, nbb) in enumerate(zip(txt_lens, num_bbs)):
            # NOTE: SEQ_LEN + Num BBOXES MUST BE < MAX_SEQ LEN for this to work! Else non singleton dimension error! 
            gather_index.data[i, tl:tl+nbb] = torch.arange(max_len, max_len+nbb, dtype=torch.long).data

        return gather_index

    def preprocess_bert(self, examples):
        """
        Copied & adapted from UNITER Repo.
        """
        iids = []
        attn_masks = []
        lmids = []

        feats_list, boxes_list = [], []

        for (i, example) in enumerate(examples):

            sent = example.sent
            feats, boxes = example.visual_feats

            sent = " ".join(str(sent).split())
            tokens = self.tokenizer.tokenize(sent)

            # Get random words
            masked_tokens, masked_label = random_word(tokens, self.tokenizer)

            tokens = ["[CLS]"] + masked_tokens + ["[SEP]"]
            input_ids = self.tokenizer.convert_tokens_to_ids(tokens)

            # LM Label
            lm_label_ids = ([-1] + masked_label + [-1] + [-1] * args.num_features)

            attn_mask = [1] * (len(input_ids) + args.num_features)

            input_ids = torch.tensor(input_ids)
            attn_mask = torch.tensor(attn_mask)
            lm_label_ids = torch.tensor(lm_label_ids)

            iids.append(input_ids)
            attn_masks.append(attn_mask)
            lmids.append(lm_label_ids)

            feats_list.append(feats)
            boxes_list.append(boxes)


        txt_lens = [i.size(0) for i in iids]

        input_ids = pad_sequence(iids, batch_first=True, padding_value=0)
        attn_masks = pad_sequence(attn_masks, batch_first=True, padding_value=0)
        lm_labels = pad_sequence(lmids, batch_first=True, padding_value=-1)
        
        img_feats = torch.from_numpy(np.stack([f for f in feats_list]))
        img_pos_feats = torch.from_numpy(np.stack([f for f in boxes_list]))
    
        # image batches
        num_bbs = [f.size(0) for f in img_feats]
        img_feats = self.pad_tensors(img_feats, num_bbs)
        img_pos_feats = self.pad_tensors(img_pos_feats, num_bbs)

        bs, max_tl = input_ids.size()
        out_size = attn_masks.size(1)
        gather_index = self.get_gather_index(txt_lens, num_bbs, bs, max_tl, out_size)

        is_matched = torch.tensor([example.is_matched for example in examples], dtype=torch.long)

        return input_ids, img_feats, img_pos_feats, attn_masks, gather_index, lm_labels, is_matched

    def forward(self, examples):

        if args.tr.startswith("bert"):
            input_ids, img_feats, img_pos_feats, attn_masks, gather_index, lm_labels, is_matched = self.preprocess_bert(examples)

        """
        forward(self, input_ids, token_type_ids=None, attention_mask=None, masked_lm_labels=None,
                visual_feats=None, pos=None, obj_labels=None, matched_label=None, ans=None):
        """

        loss, losses, ans_logit = self.model(
            input_ids.cuda(), None, img_feats.cuda(), img_pos_feats.cuda(), attn_masks.cuda(), gather_index=gather_index.cuda(),
            masked_lm_labels=lm_labels.cuda(), matched_label=is_matched.cuda()
        )

        return loss, losses.detach().cpu(), ans_logit

    def train_batch(self, optim, scheduler, batch):
        optim.zero_grad()
        loss, losses, ans_logit = self.forward(batch)
        if args.multiGPU:
            loss = loss.mean()
            losses = losses.mean(0)
        loss.backward()
        nn.utils.clip_grad_norm_(self.model.parameters(), 1.)
        optim.step()
        scheduler.step()

        return loss.item(), losses.cpu().numpy(), ans_logit

    def valid_batch(self, batch):
        with torch.no_grad():
            loss, losses, ans_logit = self.forward(batch)
            if args.multiGPU:
                loss = loss.mean()
                losses = losses.mean(0)
        return loss.item(), losses.cpu().numpy(), ans_logit


    def train(self, train_tuple: DataTuple, eval_tuple: DataTuple):
        train_ld = train_tuple.loader

        # Optimizer
        batch_per_epoch = len(train_ld)
        t_total = int(batch_per_epoch * args.epochs)
        warmup_ratio = 0.05
        warmup_iters = int(t_total * warmup_ratio)

        print("Batch per epoch: %d" % batch_per_epoch)
        print("Total Iters: %d" % t_total)
        print("Warm up Iters: %d" % warmup_iters)

        optim = AdamW(self.model.parameters(), lr=args.lr)
        #scheduler = get_linear_schedule_with_warmup(optim, warmup_iters, t_total)
        # We use cos scheduler here, as it ends smoother than linear & we take the LAST model.
        scheduler = get_cosine_schedule_with_warmup(optim, warmup_iters, t_total)

        # Train
        best_eval_loss = 9595.
        for epoch in range(args.epochs):
            # Train
            self.model.train()
            total_loss = 0.
            total_losses = 0.
            uid2ans = {}
            for batch in tqdm(train_ld, total=len(train_ld)):
                loss, losses, logit = self.train_batch(optim, scheduler, batch)
                total_loss += loss
                total_losses += losses

                if args.task_qa:
                    score, label = logit.max(1)
                    for datum, l in zip(batch, label.cpu().numpy()):
                        uid = datum.uid
                        ans = train_tuple.dataset.answer_table.id2ans(l)
                        uid2ans[uid] = ans

            print("The training loss for Epoch %d is %0.4f" % (epoch, total_loss / batch_per_epoch))
            losses_str = "The losses are "
            # Somehow had to add [0] here, which is not in or repo
            for name, loss in zip(LOSSES_NAME, total_losses[0]):
                losses_str += "%s: %0.4f " % (name, loss / batch_per_epoch)
            print(losses_str)

            if args.task_qa:
                train_tuple.evaluator.evaluate(uid2ans, pprint=True)
                
            if epoch == 5:
                self.save("Epoch%02d" % (epoch+1))

        self.save("LAST")


    def save(self, name):
        torch.save(self.model.state_dict(),
                   os.path.join(args.output, "%s_BU.pth" % name))

    def load(self, path):
        print("Load BERT extractor from %s" % path)
        state_dict = torch.load("%s" % path)
        self.model.load_state_dict(state_dict)

    def load_lxmert(self, path):
        # Load state_dict from snapshot file
        print("Load LXMERT pre-trained model from %s" % path)
        state_dict = torch.load("%s" % path)
        new_state_dict = {}
        for key, value in state_dict.items():
            
            # We need Bert keys
            key = "bert." + key


            if 'uniter.' in key:
                key = key.replace('uniter.', '')

            # Unfortuantely their models are pretrained on bert-large-cased
            # Uncommenting the following will allow using an uncased model
            #if key.startswith("embeddings.word_embeddings.weight"):
            #    print("SKIPPING:", key)
            #    continue

            if key.startswith("bert.img_embeddings.pos_linear.weight"):
                value = value[:, :4].clone()
                print("MODIFYING:", key)

            if key.startswith("module."):
                new_state_dict[key[len("module."):]] = value
            else:
                new_state_dict[key] = value
        state_dict = new_state_dict

        # Print out the differences of pre-trained and model weights.
        load_keys = set(state_dict.keys())
        model_keys = set(self.model.state_dict().keys())
        print()
        print("Weights in loaded but not in model:")
        for key in sorted(load_keys.difference(model_keys)):
            print(key)
        print()
        print("Weights in model but not in loaded:")
        for key in sorted(model_keys.difference(load_keys)):
            print(key)
        print()

        # Load weights to model
        self.model.load_state_dict(state_dict, strict=False)

if __name__ == "__main__":

    lxmert = LXMERT(max_seq_length=128)

    lxmert.train(train_tuple, valid_tuple)





    