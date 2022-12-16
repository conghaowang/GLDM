import torch
from utils import (
    safe_divide_loss,
    compute_neglogprob_for_multihot_objective,
    traced_unsorted_segment_log_softmax,
)
from model_utils import GenericMLP


distance_truncation = 10


class MLPDecoder(torch.nn.Module):
    """Returns graph level representation of the molecules."""

    def __init__(
        self,
        params,  # nested dictionary of parameters for each MLP
    ):
        super(MLPDecoder, self).__init__()
        # Node selection
        self._node_type_selector = GenericMLP(**params["node_type_selector"])
        self._node_type_loss_weights = params["node_type_loss_weights"]

        # Edge selection
        self._no_more_edges_representation = torch.nn.Parameter(
            torch.rand(*params["no_more_edges_repr"]), requires_grad=True
        )
        self._edge_candidate_scorer = GenericMLP(**params["edge_candidate_scorer"])
        self._edge_type_selector = GenericMLP(**params["edge_type_selector"])

        # Attachment Point Selection

    def pick_node_type(
        self,
        input_molecule_representations,
        graph_representations,
        graphs_requiring_node_choices,
    ):
        relevant_graph_representations = input_molecule_representations[
            graphs_requiring_node_choices
        ]
        relevant_input_molecule_representations = graph_representations[
            graphs_requiring_node_choices
        ]
        original_and_calculated_graph_representations = torch.cat(
            (relevant_graph_representations, relevant_input_molecule_representations),
            axis=-1,
        )
        node_type_logits = self._node_type_selector(
            original_and_calculated_graph_representations
        )
        return node_type_logits

    def compute_node_type_selection_loss(
        self, node_type_logits, node_type_multihot_labels
    ):
        per_node_decision_logprobs = torch.nn.functional.log_softmax(
            node_type_logits, dim=-1
        )
        # Shape: [NTP, NT + 1]

        # number of correct choices for each of the partial graphs that require node choices
        per_node_decision_num_correct_choices = torch.sum(
            node_type_multihot_labels, keepdim=True, axis=-1
        )
        # Shape [NTP, 1]

        per_correct_node_decision_normalised_neglogprob = (
            compute_neglogprob_for_multihot_objective(
                logprobs=per_node_decision_logprobs[
                    :, :-1
                ],  # separate out the no node prediction
                multihot_labels=node_type_multihot_labels,
                per_decision_num_correct_choices=per_node_decision_num_correct_choices,
            )
        )  # Shape [NTP, NT]

        no_node_decision_correct = (
            per_node_decision_num_correct_choices == 0.0
        ).sum()  # Shape [NTP]
        per_correct_no_node_decision_neglogprob = -(
            per_node_decision_logprobs[:, -1]
            * torch.squeeze(no_node_decision_correct).type(torch.FloatTensor)
        )  # Shape [NTP]

        if self._node_type_loss_weights is not None:
            per_correct_node_decision_normalised_neglogprob *= (
                self._node_type_loss_weights[:-1]
            )
            per_correct_no_node_decision_neglogprob *= self._node_type_loss_weights[-1]

        # Loss is the sum of the masked (no) node decisions, averaged over number of decisions made:
        total_node_type_loss = torch.sum(
            per_correct_node_decision_normalised_neglogprob
        ) + torch.sum(per_correct_no_node_decision_neglogprob)
        node_type_loss = safe_divide_loss(
            total_node_type_loss, node_type_multihot_labels.shape[0]
        )

        return node_type_loss

    def pick_edge(
        self,
        input_molecule_representations,
        partial_graph_representions,
        node_representations,
        num_graphs_in_batch,  # len(batch.ptr) - 1
        graph_to_focus_node_map,  # batch.focus_node
        node_to_graph_map,  # batch.batch
        candidate_edge_targets,  # batch.valid_edge_choices[:, 1]
        candidate_edge_features,  # batch.edge_features
    ):
        focus_node_representations = node_representations[graph_to_focus_node_map]

        graph_and_focus_node_representations = torch.cat(
            (
                input_molecule_representations,
                partial_graph_representions,
                focus_node_representations,
            ),
            axis=-1,
        )

        # Explanation: at each step, there is a focus node, which is the node we are
        # focusing on right now in terms of adding another edge to it. When adding a new
        # edge, the edge can be between the focus node and a variety of other nodes.
        # This is likely based on valency, and in reality, it is possible that none of the
        # edge choices are correct (when that generation step is a node addition step)
        # and not an edge addition step. Regardless, we still want to consider the candidates
        # "target" refers to the node at the other end of the candidate edge
        valid_target_to_graph_map = node_to_graph_map[candidate_edge_targets]
        graph_and_focus_node_representations_per_edge_candidate = (
            graph_and_focus_node_representations[valid_target_to_graph_map]
        )
        edge_candidate_target_node_representations = node_representations[
            candidate_edge_targets
        ]

        # The zeroth element of edge_features is the graph distance. We need to look that up
        # in the distance embeddings:
        truncated_distances = torch.minimum(
            candidate_edge_features[:, 0],
            torch.ones(len(candidate_edge_features)) * (distance_truncation - 1),
        )  # shape: [CE]

        # since we want to truncate the distance, we should have an embedding layer for it
        distance_embedding_layer = torch.nn.Embedding(distance_truncation, 1)

        distance_embedding = distance_embedding_layer(truncated_distances.long())

        # Concatenate all the node features, to form focus_node -> target_node edge features
        edge_candidate_representation = torch.cat(
            (
                graph_and_focus_node_representations_per_edge_candidate,
                edge_candidate_target_node_representations,
                distance_embedding,
                candidate_edge_features[:, 1:],
            ),
            axis=-1,
        )

        stop_edge_selection_representation = torch.cat(
            [
                graph_and_focus_node_representations,
                torch.tile(
                    self._no_more_edges_representation,
                    dims=(num_graphs_in_batch, 1),
                ),
            ],
            axis=-1,
        )  # shape: [PG, MD + PD + 2 * VD*(num_layers+1) + FD]

        edge_candidate_and_stop_features = torch.cat(
            [edge_candidate_representation, stop_edge_selection_representation], axis=0
        )  # shape: [CE + PG, MD + PD + 2 * VD*(num_layers+1) + FD]
        edge_candidate_logits = torch.squeeze(
            self._edge_candidate_scorer(edge_candidate_and_stop_features),
            axis=-1,
        )  # shape: [CE + PG]
        edge_type_logits = self._edge_type_selector(
            edge_candidate_representation
        )  # shape: [CE, ET]

        return edge_candidate_logits, edge_type_logits

    def compute_edge_candidate_selection_loss(
        self,
        num_graphs_in_batch,  # len(batch.ptr)-1
        node_to_graph_map,  # batch.batch
        candidate_edge_targets,  # batch_features["valid_edge_choices"][:, 1]
        edge_candidate_logits,  # as is
        per_graph_num_correct_edge_choices,  # batch.num_correct_edge_choices
        edge_candidate_correctness_labels,  # correct edge choices
        no_edge_selected_labels,  # stop node label
    ):

        # First, we construct full labels for all edge decisions, which are the concat of
        # edge candidate logits and the logits for choosing no edge:
        edge_correctness_labels = torch.cat(
            [edge_candidate_correctness_labels, no_edge_selected_labels.float()],
            axis=0,
        )  # Shape: [CE + PG]

        # To compute a softmax over all candidate edges (and the "no edge" choice) corresponding
        # to the same graph, we first need to build the map from each logit to the corresponding
        # graph id. Then, we can do an unsorted_segment_softmax using that map:
        edge_candidate_to_graph_map = node_to_graph_map[candidate_edge_targets]
        # add the end bond labels to the end
        edge_candidate_to_graph_map = torch.cat(
            (edge_candidate_to_graph_map, torch.arange(0, num_graphs_in_batch))
        )

        edge_candidate_logprobs = traced_unsorted_segment_log_softmax(
            logits=edge_candidate_logits,
            segment_ids=edge_candidate_to_graph_map,
        )  # Shape: [CE + PG]

        # Compute the edge loss with the multihot objective.
        # For a single graph with three valid choices (+ stop node) of which two are correct,
        # we may have the following:
        #  edge_candidate_logprobs = log([0.05, 0.5, 0.4, 0.05])
        #  per_graph_num_correct_edge_choices = [2]
        #  edge_candidate_correctness_labels = [0.0, 1.0, 1.0]
        #  edge_correctness_labels = [0.0, 1.0, 1.0, 0.0]
        # To get the loss, we simply look at the things in edge_candidate_logprobs that correspond
        # to correct entries.
        # However, to account for the _multi_hot nature, we scale up each entry of
        # edge_candidate_logprobs by the number of correct choices, i.e., consider the
        # correct entries of
        #  log([0.05, 0.5, 0.4, 0.05]) + log([2, 2, 2, 2]) = log([0.1, 1.0, 0.8, 0.1])
        # In this form, we want to have each correct entry to be as near possible to 1.
        # Finally, we normalise loss contributions to by-graph, by dividing the crossentropy
        # loss by the number of correct choices (i.e., in the example above, this results in
        # a loss of -((log(1.0) + log(0.8)) / 2) = 0.11...).

        # Note: per_graph_num_correct_edge_choices does not include the choice of an edge to
        # the stop node, so can be zero.
        per_graph_num_correct_edge_choices = torch.max(
            per_graph_num_correct_edge_choices,
            torch.ones(per_graph_num_correct_edge_choices.shape),
        )  # Shape: [PG]

        per_edge_candidate_num_correct_choices = per_graph_num_correct_edge_choices[
            edge_candidate_to_graph_map
        ]
        # Shape: [CE]
        per_correct_edge_neglogprob = -(
            (
                edge_candidate_logprobs
                + torch.log(per_edge_candidate_num_correct_choices)
            )
            * edge_correctness_labels
            / per_edge_candidate_num_correct_choices
        )  # Shape: [CE]

        # Normalise by number of graphs for which we made edge selection decisions:
        edge_loss = safe_divide_loss(
            torch.sum(per_correct_edge_neglogprob), num_graphs_in_batch
        )

        return edge_loss

    def compute_decoder_loss(self, node_type_logits, node_type_multihot_labels):
        # Compute node selection loss
        node_selection_loss = self.compute_node_type_selection_loss(
            node_type_logits, node_type_multihot_labels
        )

        # Compute edge selection loss

        # Compute attachement point selection loss

        # Weighted sum of the losses and return it for backpropagation in
        # the lightning module
        return node_selection_loss

    def forward(
        self,
        input_molecule_representations,
        graph_representations,
        graphs_requiring_node_choices,
    ):
        # Compute node logits
        node_logits = self.pick_node_type(
            input_molecule_representations,
            graph_representations,
            graphs_requiring_node_choices,
        )

        # Compute edge logits

        # Compute attachment point logits

        # return all logits
        return node_logits
