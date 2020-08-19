import torch
from torch import nn
from torch.nn import Module, Linear, LayerNorm
from transformers import BertPreTrainedModel, LongformerModel, LongformerConfig
from transformers.modeling_bert import ACT2FN


class FullyConnectedLayer(Module):
    # TODO: many layers
    def __init__(self, config, input_dim, output_dim):
        super(FullyConnectedLayer, self).__init__()

        self.input_dim = input_dim
        self.output_dim = output_dim

        self.dense = Linear(self.input_dim, self.output_dim)
        self.layer_norm = LayerNorm(self.output_dim, eps=config.layer_norm_eps)
        self.activation_func = ACT2FN[config.hidden_act]

    def forward(self, inputs):
        temp = inputs
        temp = self.dense(temp)
        temp = self.activation_func(temp)
        temp = self.layer_norm(temp)
        return temp


class LongformerForCoreferenceResolution(BertPreTrainedModel):
    config_class = LongformerConfig
    base_model_prefix = "longformer"

    def __init__(self, config, antecedent_loss):
        super().__init__(config)
        self.num_labels = config.num_labels
        self.antecedent_loss = antecedent_loss  # can be either allowed loss or bce

        self.longformer = LongformerModel(config)

        self.start_mention_mlp = FullyConnectedLayer(config, config.hidden_size, config.hidden_size)
        self.end_mention_mlp = FullyConnectedLayer(config, config.hidden_size, config.hidden_size)
        self.start_coref_mlp = FullyConnectedLayer(config, config.hidden_size, config.hidden_size)
        self.end_coref_mlp = FullyConnectedLayer(config, config.hidden_size, config.hidden_size)

        self.entity_mention_start_classifier = nn.Linear(config.hidden_size, 1)  # In paper w_s
        self.entity_mention_end_classifier = nn.Linear(config.hidden_size, 1)  # w_e
        self.entity_mention_joint_classifier = nn.Linear(config.hidden_size, config.hidden_size)  # M

        self.antecedent_start_classifier = nn.Linear(config.hidden_size, config.hidden_size)  # S
        self.antecedent_end_classifier = nn.Linear(config.hidden_size, config.hidden_size)  # E

        self.init_weights()

    def _compute_entity_mention_loss(self, start_entity_mention_labels, end_entity_mention_labels, mention_logits,
                                     attention_mask=None):
        """
        :param start_entity_mention_labels: [batch_size, num_mentions]
        :param end_entity_mention_labels: [batch_size, num_mentions]
        :param mention_logits: [batch_size, seq_length, seq_length]
        :return:
        """
        device = start_entity_mention_labels.device
        batch_size, seq_length, _ = mention_logits.size()
        num_mentions = start_entity_mention_labels.size(-1)

        # We now take the index tensors and turn them into sparse tensors
        full_entity_mentions = torch.zeros(size=(batch_size, seq_length, seq_length), device=device)
        batch_temp = torch.arange(batch_size, device=device).unsqueeze(-1).repeat(1, num_mentions)
        full_entity_mentions[
            batch_temp, start_entity_mention_labels, end_entity_mention_labels] = 1.0  # [batch_size, seq_length, seq_length]
        full_entity_mentions[:, 0, 0] = 0.0  # Remove the padded mentions

        weights = (attention_mask.unsqueeze(-1) & attention_mask.unsqueeze(-2))

        num_positive_labels = torch.sum(full_entity_mentions)
        num_negative_labels = torch.sum(1 - full_entity_mentions) - torch.sum(1 - weights)
        loss_fct = nn.BCEWithLogitsLoss(weight=weights, pos_weight=num_negative_labels / num_positive_labels)
        loss = loss_fct(mention_logits, full_entity_mentions)

        return loss

    def _prepare_antecedent_matrix(self, antecedent_labels, seq_length):
        """
        :param antecedent_labels: [batch_size, seq_length, cluster_size]
        :return: [batch_size, seq_length, seq_length]
        """
        device = antecedent_labels.device
        batch_size, num_mentions, cluster_size = antecedent_labels.size()

        # We now prepare a tensor with the gold antecedents for each span
        labels = torch.zeros(size=(batch_size, seq_length, seq_length), device=device)
        batch_temp = torch.arange(batch_size, device=device).unsqueeze(-1).unsqueeze(-1).repeat(1, seq_length,
                                                                                                cluster_size)
        seq_length_temp = torch.arange(seq_length, device=device).unsqueeze(0).unsqueeze(-1).repeat(batch_size, 1,
                                                                                                    cluster_size)

        labels[batch_temp, seq_length_temp, antecedent_labels] = 1.0
        labels[:, :, -1] = 0.0  # Fix all pad-antecedents

        return labels

    def _compute_antecedent_loss(self, antecedent_labels, antecedent_logits, attention_mask=None):
        """
        :param antecedent_labels: [batch_size, seq_length, cluster_size]
        :param antecedent_logits: [batch_size, seq_length, seq_length]
        :param attention_mask: [batch_size, seq_length]
        """
        seq_length = antecedent_logits.size(-1)
        labels = self._prepare_antecedent_matrix(antecedent_labels, seq_length)  # [batch_size, seq_length, seq_length]
        gold_antecedent_logits = antecedent_logits + ((1 - labels) * -1e8)

        if self.antecedent_loss == "allowed":
            only_positive_labels = labels.clone()
            only_positive_labels[:, :, 0] = 0
            num_positive_examples = torch.sum(only_positive_labels)
            num_negative_examples = torch.sum(labels[:, :, 0]) - torch.sum(1 - attention_mask)
            pos_weight = num_negative_examples / num_positive_examples

            gold_log_sum_exp = torch.logsumexp(gold_antecedent_logits, dim=-1)  # [batch_size, seq_length]
            all_log_sum_exp = torch.logsumexp(antecedent_logits, dim=-1)  # [batch_size, seq_length]

            gold_log_probs = gold_log_sum_exp - all_log_sum_exp
            losses = -gold_log_probs

            loss_weights = (torch.sum(only_positive_labels, dim=-1) * (pos_weight - 1)) + 1
            losses = losses * loss_weights

            attention_mask_to_add = torch.zeros_like(attention_mask)
            attention_mask_to_add[:, 0] = -1
            attention_mask = attention_mask + attention_mask_to_add
            sum_losses = torch.sum(losses * attention_mask)
            num_examples = torch.sum(attention_mask)
            loss = sum_losses / (num_examples + 1e-8)
        else:  # == bce
            weight = torch.tril(torch.ones(labels.size()))
            weight[:, 0, 0] = 1
            weight = weight * attention_mask.unsqueeze(1)
            pos_indices = labels * weight
            pos_num = torch.sum(pos_indices)
            neg_indices = (1 - labels) * weight
            neg_num = torch.sum(neg_indices)
            loss_fct = nn.BCEWithLogitsLoss(weight=weight, pos_weight=neg_num / pos_num)
            loss = loss_fct(antecedent_logits, labels)

        return loss

    def mask_antecedent_logits(self, antecedent_logits):
        antecedents_mask = torch.ones_like(antecedent_logits).triu() * (-1e8)  # [batch_size, seq_length, seq_length]
        antecedents_mask[:, 0, 0] = 0
        antecedent_logits = antecedent_logits + antecedents_mask  # [batch_size, seq_length, seq_length]
        return antecedent_logits

    def forward(self, input_ids, attention_mask=None, start_entity_mention_labels=None, end_entity_mention_labels=None,
                start_antecedent_labels=None, end_antecedent_labels=None, return_all_outputs=False):
        outputs = self.longformer(input_ids, attention_mask=attention_mask)
        sequence_output = outputs[0]

        # Compute representations
        start_mention_reps = self.start_mention_mlp(sequence_output)
        end_mention_reps = self.end_mention_mlp(sequence_output)
        start_coref_reps = self.start_coref_mlp(sequence_output)
        end_coref_reps = self.end_coref_mlp(sequence_output)

        # Entity mention scores
        start_mention_logits = self.entity_mention_start_classifier(start_mention_reps).squeeze(
            -1)  # [batch_size, seq_length]
        end_mention_logits = self.entity_mention_end_classifier(end_mention_reps).squeeze(
            -1)  # [batch_size, seq_length]

        temp = self.entity_mention_joint_classifier(start_mention_reps)  # [batch_size, seq_length]
        joint_mention_logits = torch.matmul(temp,
                                            end_mention_reps.permute([0, 2, 1]))  # [batch_size, seq_length, seq_length]

        mention_logits = joint_mention_logits + start_mention_logits.unsqueeze(-1) + end_mention_logits.unsqueeze(-2)

        # Antecedent scores
        temp = self.antecedent_start_classifier(start_coref_reps)  # [batch_size, seq_length, dim]
        start_coref_logits = torch.matmul(temp,
                                          start_coref_reps.permute([0, 2, 1]))  # [batch_size, seq_length, seq_length]
        start_coref_logits = self.mask_antecedent_logits(start_coref_logits)
        temp = self.antecedent_end_classifier(end_coref_reps)  # [batch_size, seq_length, dim]
        end_coref_logits = torch.matmul(temp, end_coref_reps.permute([0, 2, 1]))  # [batch_size, seq_length, seq_length]
        end_coref_logits = self.mask_antecedent_logits(end_coref_logits)


        outputs = outputs[2:]
        if return_all_outputs:
            outputs = (mention_logits, start_coref_logits, end_coref_logits) + outputs

        if start_entity_mention_labels is not None and end_entity_mention_labels is not None \
                and start_antecedent_labels is not None and end_antecedent_labels is not None:
            entity_mention_loss = self._compute_entity_mention_loss(
                start_entity_mention_labels=start_entity_mention_labels,
                end_entity_mention_labels=end_entity_mention_labels,
                mention_logits=mention_logits,
                attention_mask=attention_mask)
            start_coref_loss = self._compute_antecedent_loss(antecedent_labels=start_antecedent_labels,
                                                             antecedent_logits=start_coref_logits,
                                                             attention_mask=attention_mask)
            end_coref_loss = self._compute_antecedent_loss(antecedent_labels=end_antecedent_labels,
                                                           antecedent_logits=end_coref_logits,
                                                           attention_mask=attention_mask)
            loss = entity_mention_loss + start_coref_loss + end_coref_loss
            outputs = (loss,) + outputs

        return outputs
