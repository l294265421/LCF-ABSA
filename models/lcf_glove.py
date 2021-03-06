# -*- coding: utf-8 -*-
# file: lcf_glove.py
# author: yangheng <yangheng@m.scnu.edu.cn>
# Copyright (C) 2019. All Rights Reserved.

import torch
import torch.nn as nn

import numpy as np
from layers.point_wise_feed_forward import PositionwiseFeedForward
from pytorch_transformers.modeling_bert import BertPooler, BertSelfAttention, BertConfig


class SelfAttention(nn.Module):
    def __init__(self, config, opt):
        super(SelfAttention, self).__init__()
        self.opt = opt
        self.config = config
        self.SA = BertSelfAttention(config)
        self.tanh = torch.nn.Tanh()

    def forward(self, input):
        zero_tensor = torch.tensor(np.zeros((input.size(0), 1, 1, self.opt.max_seq_len),
                                            dtype=np.float32), dtype=torch.float32).to(self.opt.device)
        SA_out = self.SA(input, zero_tensor)
        return self.tanh(SA_out[0])

class LCF_GLOVE(nn.Module):

    def __init__(self, embedding_matrix, opt):
        super(LCF_GLOVE, self).__init__()
        self.config = BertConfig.from_json_file("config.json")
        self.opt = opt
        self.embed = nn.Embedding.from_pretrained(torch.tensor(embedding_matrix, dtype=torch.float))
        self.mha_global = SelfAttention(self.config, opt)
        self.mha_local = SelfAttention(self.config, opt)
        self.ffn_global = PositionwiseFeedForward(self.opt.hidden_dim, dropout=self.opt.dropout)
        self.ffn_local = PositionwiseFeedForward(self.opt.hidden_dim, dropout=self.opt.dropout)
        self.mha_local_SA = SelfAttention(self.config, opt)
        self.mha_global_SA = SelfAttention(self.config, opt)
        self.mha_SA_single = SelfAttention(self.config, opt)
        self.bert_pooler = BertPooler(self.config)

        self.bert_pooler1 = BertPooler(self.config)
        self.bert_pooler2 = BertPooler(self.config)
        self.dense1=nn.Linear(opt.hidden_dim, opt.polarities_dim)
        self.dense2=nn.Linear(opt.hidden_dim, opt.polarities_dim)
        self.sentiment_pool = nn.Linear(6, 3)

        self.dropout = nn.Dropout(opt.dropout)
        self.mean_pooling_double = nn.Linear(opt.embed_dim * 2, opt.hidden_dim)
        self.mean_pooling_single = nn.Linear(opt.embed_dim, opt.hidden_dim)
        self.dense = nn.Linear(opt.hidden_dim, opt.polarities_dim)

    # create the mask tensor for local context features
    def feature_dynamic_mask(self, text_local_indices, aspect_indices):
        texts = text_local_indices.cpu().numpy()
        asps = aspect_indices.cpu().numpy()
        mask_len = self.opt.SRD
        masked_text_raw_indices = np.ones((text_local_indices.size(0), self.opt.max_seq_len, self.opt.hidden_dim),
                                          dtype=np.float32)
        for text_i, asp_i in zip(range(len(texts)), range(len(asps))):
            asp_len = np.count_nonzero(asps[asp_i]) - 2
            try:
                asp_begin = np.argwhere(texts[text_i] == asps[asp_i][1])[0][0]
            except:
                continue
            if asp_begin >= mask_len:
                mask_begin = asp_begin - mask_len
            else:
                mask_begin = 0
            for i in range(mask_begin):
                masked_text_raw_indices[text_i][i] = np.zeros((self.opt.hidden_dim), dtype=np.float)
            for j in range(asp_begin + asp_len + mask_len + 1, self.opt.max_seq_len):
                masked_text_raw_indices[text_i][j] = np.zeros((self.opt.hidden_dim), dtype=np.float)
        masked_text_raw_indices = torch.from_numpy(masked_text_raw_indices)
        return masked_text_raw_indices.to(self.opt.device)

    # create the weights tensor for local context features
    def feature_dynamic_weighted(self, text_local_indices, aspect_indices):
        texts = text_local_indices.cpu().numpy()
        asps = aspect_indices.cpu().numpy()
        masked_text_raw_indices = np.ones((text_local_indices.size(0), self.opt.max_seq_len, self.opt.hidden_dim),
                                          dtype=np.float32)
        for text_i, asp_i in zip(range(len(texts)), range(len(asps))):
            asp_len = np.count_nonzero(asps[asp_i]) - 2
            try:
                asp_begin = np.argwhere(texts[text_i] == asps[asp_i][1])[0][0]
                asp_avg_index = (asp_begin * 2 + asp_len) / 2
            except:
                continue
            distances = np.zeros(np.count_nonzero(texts[text_i]), dtype=np.float32)
            for i in range(1, np.count_nonzero(texts[text_i]) - 1):
                if abs(i - asp_avg_index) + asp_len / 2 > self.opt.SRD:
                    distances[i] = 1 - (abs(i - asp_avg_index) + asp_len / 2
                                        - self.opt.SRD) / np.count_nonzero(texts[text_i])
                else:
                    distances[i] = 1
            for i in range(len(distances)):
                masked_text_raw_indices[text_i][i] = masked_text_raw_indices[text_i][i] * distances[i]
        masked_text_raw_indices = torch.from_numpy(masked_text_raw_indices)
        return masked_text_raw_indices.to(self.opt.device)

    def forward(self, inputs):
        if self.opt.local_context_focus == 'cdm':
            text_global_indices = inputs[0]
            text_local_indices = inputs[2]
            aspect_indices = inputs[3]
        else:
            text_global_indices = inputs[2]
            text_local_indices = inputs[2]
            aspect_indices = inputs[3]

        # embedding layer
        text_global_out = self.embed(text_global_indices)
        text_local_out = self.embed(text_local_indices)

        # PFE layer
        text_global_out = self.mha_global(text_global_out)
        text_local_out = self.mha_local(text_local_out)
        text_global_out = self.ffn_global(text_global_out)
        text_local_out = self.ffn_local(text_local_out)

        # dropout
        text_global_out = self.dropout(text_global_out).to(self.opt.device)
        text_local_out = self.dropout(text_local_out).to(self.opt.device)

        # LCF layer
        if self.opt.local_context_focus == 'cdm':
            masked_text_local_features = self.feature_dynamic_mask(text_local_indices, aspect_indices)
            text_local_out = torch.mul(text_local_out, masked_text_local_features)
        elif self.opt.local_context_focus == 'cdw':
            masked_text_local_features = self.feature_dynamic_weighted(text_local_indices, aspect_indices)
            text_local_out = torch.mul(text_local_out, masked_text_local_features)
        elif self.opt.local_context_focus == 'lcf_fusion':
            masked_local_text_vec = self.feature_dynamic_mask(text_local_indices, aspect_indices)
            bert_masked_local_out = torch.mul(text_global_out, masked_local_text_vec)
            weighted_text_local_features = self.feature_dynamic_weighted(text_local_indices, aspect_indices)
            bert_weighted_local_out = torch.mul(text_global_out, weighted_text_local_features)
            out_cat = torch.cat((bert_masked_local_out, text_global_out, bert_weighted_local_out), dim=-1)
            text_local_out = self.linear_triple_lcf_global(out_cat)

        local_out = self.mha_local_SA(text_local_out)
        global_out = self.mha_global_SA(text_global_out)
        # FIL layer
        cat_out = torch.cat((local_out, global_out), dim=-1)
        cat_out = self.mean_pooling_double(cat_out)
        cat_out = self.mha_SA_single(cat_out)

        # output layer
        pooled_out = self.bert_pooler(cat_out)

        dense_out = self.dense(pooled_out)
        return dense_out
