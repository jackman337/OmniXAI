#
# Copyright (c) 2023 salesforce.com, inc.
# All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# For full license text, see the LICENSE file in the repo root or https://opensource.org/licenses/BSD-3-Clause
#
"""
The explainer based ChatGPT.
"""
import os
import openai
from typing import Callable, List
from omnixai.data.tabular import Tabular
from omnixai.explainers.base import ExplainerBase
from omnixai.explainers.tabular.agnostic.shap import ShapTabular
from omnixai.explainers.tabular.counterfactual.mace.mace import MACEExplainer
from omnixai.explanations.base import PlainText


class GPTExplainer(ExplainerBase):
    """
    The explainer based on ChatGPT. The input prompt consists of the feature importance scores
    and the counterfactual examples (if used). The explanations will be the text generated by
    ChatGPT.
    """
    explanation_type = "local"
    alias = ["gpt"]

    def __init__(
            self,
            training_data: Tabular,
            predict_function: Callable,
            mode: str = "classification",
            ignored_features: List = None,
            include_counterfactual: bool = True,
            **kwargs
    ):
        """
        :param training_data: The data used to initialize a SHAP explainer. ``training_data``
            can be the training dataset for training the machine learning model.
        :param predict_function: The prediction function corresponding to the model to explain.
            When the model is for classification, the outputs of the ``predict_function``
            are the class probabilities. When the model is for regression, the outputs of
            the ``predict_function`` are the estimated values.
        :param mode: The task type, e.g., `classification` or `regression`.
        :param ignored_features: The features ignored in computing feature importance scores.
        :param include_counterfactual: Whether to include counterfactual explanations in the results.
        :param kwargs: Additional parameters to initialize `shap.KernelExplainer`, e.g., ``nsamples``.
            Please refer to the doc of `shap.KernelExplainer`.
        """
        super().__init__()
        self.shap_explainer = ShapTabular(
            training_data=training_data,
            predict_function=predict_function,
            mode=mode,
            ignored_features=ignored_features,
            nsamples=150
        )
        if include_counterfactual and mode == "classification":
            self.mace_explainer = MACEExplainer(
                training_data=training_data,
                predict_function=predict_function,
                mode=mode,
                ignored_features=ignored_features,
            )
        else:
            self.mace_explainer = None

    @staticmethod
    def _generate_prompt(
            shap_explanation,
            mace_explanation=None,
            mode="classification",
            top_k=50
    ):
        system_prompt = \
            f"You are an assistant for explaining prediction results generated " \
            f"by a machine learning {mode} model. " \
            f"Your decisions must always be made independently without seeking user assistance. " \
            f"Your answers should be detailed and accurate for users to " \
            f"understand why the model makes such predictions."

        prompts = []
        for i, (feature, value, score) in enumerate(zip(
                shap_explanation["features"], shap_explanation["values"], shap_explanation["scores"])):
            if i < top_k:
                prompts.append('{}. "{} = {}": {:.4f}'.format(i, feature, value, score))
        context_prompt = 'Firstly, given the following feature importance scores in the format ' \
                         '"<feature name>: <feature importance score>":\n\n' + "\n".join(prompts)

        if mode == "classification":
            question_prompt = f"Please explain why this example is classified as " \
                              f"label_{shap_explanation['target_label']}."
        else:
            question_prompt = "Please explain why this example has this predicted value."
        context_prompt += f"\n\n{question_prompt}" + "\nYour answer should be concise and accurate."

        if mace_explanation is not None and mace_explanation["counterfactual"] is not None:
            df = mace_explanation["query"]
            cfs = mace_explanation["counterfactual"]
            feature_names = list(df.columns)
            feature_values = df.values[0]
            cf_label = cfs["label"].values[0]

            prompts = []
            for i, values in enumerate(cfs.values):
                changed_features = []
                for name, x, y in zip(feature_names, feature_values, values):
                    if name != "label" and x != y:
                        changed_features.append(f'"{name}" = "{y}"')
                if len(changed_features) > 0:
                    prompts.append("{}. If {}, then the predicted label will be label_{} instead of label_{}".format(
                        i, " and ".join(changed_features), cf_label, shap_explanation['target_label']))
            mace_prompt = \
                "Then given the following results generated by the MACE counterfactual explainer:" \
                "\n\n{}".format("\n".join(prompts[:2]))
            context_prompt += \
                "\n\n" + mace_prompt + "\n\n" + \
                "Please show how to change feature values to change the predicted label. " \
                "\nYour answer should be concise and accurate."

        return system_prompt, context_prompt

    @staticmethod
    def _api_call(prompt, apikey, model):
        openai.api_key = os.getenv("OPENAI_API_KEY") if not apikey else apikey
        if not openai.api_key:
            raise RuntimeError("Please set your OpenAI API KEY.")

        completion = openai.ChatCompletion.create(
            model=model,
            messages=[
                {"role": "system", "content": prompt[0]},
                {"role": "user", "content": prompt[1]}
            ]
        )
        return completion.choices[0].message.content

    def explain(
            self,
            X,
            apikey="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",
            model="gpt-3.5-turbo",
            **kwargs
    ) -> PlainText:
        explanations = PlainText()
        shap_explanations = self.shap_explainer.explain(X, nsamples=100)
        mace_explanations = self.mace_explainer.explain(X) if self.mace_explainer is not None else None

        for i, e in enumerate(shap_explanations.get_explanations()):
            mace = mace_explanations.get_explanations()[i] if mace_explanations is not None else None
            input_prompt = self._generate_prompt(e, mace_explanation=mace)
            explanation = self._api_call(input_prompt, apikey, model)
            explanations.add(instance=X.iloc(i).to_pd(), text=explanation)
        return explanations
