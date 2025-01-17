import os
import sys
from typing import List
import json
import fire
import torch
from torch.utils.data import DataLoader
import transformers
from datasets import load_dataset, concatenate_datasets, Dataset
from transformers import TrainerCallback, TrainingArguments, TrainerState, TrainerControl
from transformers.trainer_utils import PREFIX_CHECKPOINT_DIR
from peft import (
    LoraConfig,
    prepare_model_for_int8_training,
    set_peft_model_state_dict,
    MODEL_TYPE_TO_PEFT_MODEL_MAPPING,
    PeftModel,
)
from peft.utils import _prepare_prompt_learning_config
from transformers.tokenization_utils_base import PreTrainedTokenizerBase
from transformers.utils import PaddingStrategy
from transformers import LlamaTokenizer, LlamaConfig, AutoTokenizer
from modeling_llama_unlikelihood_dynamic_correct import NLFTLlamaForCausalLM, NLFTPeftModelForCausalLM
from prompter import Prompter
from typing import Optional, Union, Any
from dataclasses import dataclass
import numpy as np
import random
import pdb
from transformers import BitsAndBytesConfig

os.environ["TOKENIZERS_PARALLELISM"] = "false"


@dataclass
class MyDataCollator:
    """
    Data collator that will dynamically pad the inputs received, as well as the labels.

    Args:
        tokenizer ([`PreTrainedTokenizer`] or [`PreTrainedTokenizerFast`]):
            The tokenizer used for encoding the data.
        model ([`PreTrainedModel`]):
            The model that is being trained. If set and has the *prepare_decoder_input_ids_from_labels*, use it to
            prepare the *decoder_input_ids*

            This is useful when using *label_smoothing* to avoid calculating loss twice.
        padding (`bool`, `str` or [`~utils.PaddingStrategy`], *optional*, defaults to `True`):
            Select a strategy to pad the returned sequences (according to the model's padding side and padding index)
            among:

            - `True` or `'longest'` (default): Pad to the longest sequence in the batch (or no padding if only a single
              sequence is provided).
            - `'max_length'`: Pad to a maximum length specified with the argument `max_length` or to the maximum
              acceptable input length for the model if that argument is not provided.
            - `False` or `'do_not_pad'`: No padding (i.e., can output a batch with sequences of different lengths).
        max_length (`int`, *optional*):
            Maximum length of the returned list and optionally padding length (see above).
        pad_to_multiple_of (`int`, *optional*):
            If set will pad the sequence to a multiple of the provided value.

            This is especially useful to enable the use of Tensor Cores on NVIDIA hardware with compute capability >=
            7.5 (Volta).
        label_pad_token_id (`int`, *optional*, defaults to -100):
            The id to use when padding the labels (-100 will be automatically ignored by PyTorch loss functions).
        return_tensors (`str`):
            The type of Tensor to return. Allowable values are "np", "pt" and "tf".
    """
    tokenizer: PreTrainedTokenizerBase
    model: Optional[Any] = None
    padding: Union[bool, str, PaddingStrategy] = True
    max_length: Optional[int] = None
    pad_to_multiple_of: Optional[int] = None
    label_pad_token_id: int = -100
    return_tensors: str = "pt"

    def __call__(self, features, return_tensors=None):
        
        if return_tensors is None:
            return_tensors = self.return_tensors
        labels = [feature["labels"] for feature in features] if "labels" in features[0].keys() else None
        labels_wrap = [feature["labels_wrap"] for feature in features] if "labels_wrap" in features[0].keys() else None
        labels_correct = [feature["labels_correct"] for feature in features] if "labels_correct" in features[0].keys() else None
        # We have to pad the labels before calling `tokenizer.pad` as this method won't pad them and needs them of the
        # same length to return tensors.

        # 补 bug
        if labels is not None and labels_wrap is None:
            max_label_length = max(len(l) for l in labels)
        if labels is not None and labels_wrap is not None:
            max_label_length = max(len(l) for l in labels)
            max_label_length_wrap = max(len(l) for l in labels_wrap)
            max_label_length_correct = max(len(l) for l in labels_correct)
            max_label_length = max(max_label_length, max_label_length_wrap,max_label_length_correct)
            if self.pad_to_multiple_of is not None:
                max_label_length = (
                        (max_label_length + self.pad_to_multiple_of - 1)
                        // self.pad_to_multiple_of
                        * self.pad_to_multiple_of
                )
            # self.tokenizer.padding_side = "left"
            padding_side = self.tokenizer.padding_side

            for feature in features:
                # feature['weight'] = [feature['weight']]
                # feature['polarity'] = [feature['polarity']]
                remainder = [self.label_pad_token_id] * (max_label_length - len(feature["labels"]))
                remainder_length = max_label_length - len(feature["labels_wrap"])
                remainder_label = [self.label_pad_token_id] * remainder_length
                remainder_ids = [self.tokenizer.pad_token_id] * remainder_length
                remainder_mask = [0] * remainder_length

                remainder_length_correct = max_label_length - len(feature["labels_correct"])
                remainder_label_correct = [self.label_pad_token_id] * remainder_length_correct
                remainder_ids_correct = [self.tokenizer.pad_token_id] * remainder_length_correct
                remainder_mask_correct = [0] * remainder_length_correct
                # pdb.set_trace()
                if isinstance(feature["labels"], list):
                    feature["labels"] = (
                        feature["labels"] + remainder if padding_side == "right" else remainder + feature["labels"]
                    )
                    feature["labels_wrap"] = (
                        feature["labels_wrap"] + remainder_label if padding_side == "right" else remainder_label + feature["labels_wrap"]
                    )
                    feature["input_ids_wrap"] = (
                        feature["input_ids_wrap"] + remainder_ids if padding_side == "right" else remainder_ids + feature["input_ids_wrap"]
                    )
                    feature["attention_mask_wrap"] = (
                        feature["attention_mask_wrap"] + remainder_mask if padding_side == "right" else remainder_mask + feature["attention_mask_wrap"]
                    )
                    feature["labels_correct"] = (
                        feature["labels_correct"] + remainder_label_correct if padding_side == "right" else remainder_label_correct + feature["labels_correct"]
                    )
                    feature["input_ids_correct"] = (
                        feature["input_ids_correct"] + remainder_ids_correct if padding_side == "right" else remainder_ids_correct + feature["input_ids_correct"]
                    )
                    feature["attention_mask_correct"] = (
                        feature["attention_mask_correct"] + remainder_mask_correct if padding_side == "right" else remainder_mask_correct + feature["attention_mask_correct"]
                    )
                elif padding_side == "right":
                    feature["labels"] = np.concatenate([feature["labels"], remainder]).astype(np.int64)
                    feature["labels_wrap"] = np.concatenate([feature["labels_wrap"], remainder_label]).astype(np.int64)
                    feature["input_ids_wrap"] = np.concatenate([feature["input_ids_wrap"], remainder_ids]).astype(np.int64)
                    feature["attention_mask_wrap"] = np.concatenate([feature["attention_mask_wrap"], remainder_mask]).astype(np.int64)
                    feature["labels_correct"] = np.concatenate([feature["labels_correct"], remainder_label_correct]).astype(np.int64)
                    feature["input_ids_correct"] = np.concatenate([feature["input_ids_correct"], remainder_ids_correct]).astype(np.int64)
                    feature["attention_mask_correct"] = np.concatenate([feature["attention_mask_correct"], remainder_mask_correct]).astype(np.int64)
                else:
                    feature["labels"] = np.concatenate([remainder, feature["labels"]]).astype(np.int64)
                    feature["labels_wrap"] = np.concatenate([remainder_label, feature["labels_wrap"]]).astype(np.int64)
                    feature["input_ids_wrap"] = np.concatenate([remainder_ids, feature["input_ids_wrap"]]).astype(np.int64)
                    feature["attention_mask_wrap"] = np.concatenate([remainder_mask, feature["attention_mask_wrap"]]).astype(np.int64)
                    feature["labels_correct"] = np.concatenate([remainder_label_correct, feature["labels_correct"]]).astype(np.int64)
                    feature["input_ids_correct"] = np.concatenate([remainder_ids_correct, feature["input_ids_correct"]]).astype(np.int64)
                    feature["attention_mask_correct"] = np.concatenate([remainder_mask_correct, feature["attention_mask_correct"]]).astype(np.int64)

        features = self.tokenizer.pad(
            features,
            padding=self.padding,
            max_length=max_label_length,
            pad_to_multiple_of=self.pad_to_multiple_of,
            return_tensors=return_tensors,
        )

        # prepare decoder_input_ids
        if (
            labels is not None
            and self.model is not None
            and hasattr(self.model, "prepare_decoder_input_ids_from_labels")
        ):
            decoder_input_ids = self.model.prepare_decoder_input_ids_from_labels(labels=features["labels"])
            features["decoder_input_ids"] = decoder_input_ids
        return features


# 每个 epoch 结束后，保存 lora
class SavePeftModelOnEpochEndCallback(TrainerCallback):
    def __init__(self, output_dir):
        self.output_dir = output_dir

    def on_epoch_end(self, args, state, control, model=None, **kwargs):
        checkpoint_folder = os.path.join(self.output_dir, f"{PREFIX_CHECKPOINT_DIR}_epoch_{state.epoch}")
        model.save_pretrained(checkpoint_folder)
        return control


def read_json(addr):
    with open(addr) as reader:
        f = json.load(reader)
    return f


def read_jsonl(filename):
    with open(filename, 'r') as file:
        # Read the lines in the file and convert each line from JSON to a Python dict
        return [json.loads(line) for line in file]


def get_peft_model(model, peft_config, adapter_name: str = "default"):
    """
    Returns a Peft model object from a model and a config.

    Args:
        model ([`transformers.PreTrainedModel`]): Model to be wrapped.
        peft_config ([`PeftConfig`]): Configuration object containing the parameters of the Peft model.
    """
    model_config = getattr(model, "config", {"model_type": "custom"})
    if hasattr(model_config, "to_dict"):
        model_config = model_config.to_dict()

    peft_config.base_model_name_or_path = model.__dict__.get("name_or_path", None)

    if peft_config.task_type not in MODEL_TYPE_TO_PEFT_MODEL_MAPPING.keys() and not peft_config.is_prompt_learning:
        return PeftModel(model, peft_config, adapter_name=adapter_name)
    if peft_config.is_prompt_learning:
        peft_config = _prepare_prompt_learning_config(peft_config, model_config)
    return NLFTPeftModelForCausalLM(model, peft_config, adapter_name=adapter_name)


def train(
    # model/data params
    base_model: str = "",
    data_path: str = "",
    output_dir: str = "",
    # training hyperparams
    batch_size: int = 128,
    micro_batch_size: int = 8,
    num_epochs: int = 1,
    learning_rate: float = 3e-4,
    cutoff_len: int = 4096,
    val_set_size: int = 0,
    lr_scheduler: str = "cosine",
    # lora hyperparams
    lora_r: int = 16,
    lora_alpha: int = 16,
    lora_dropout: float = 0.05,
    # from peft docs: ["q_proj", "k_proj", "v_proj", "o_proj", "fc_in", "fc_out", "wte", "gate_proj", "down_proj", "up_proj"]
    lora_target_modules: List[str] = ["gate_proj", "down_proj", "up_proj"],
    # llm hyperparams
    train_on_inputs: bool = False,  # if False, masks out inputs in loss
    add_eos_token: bool = False,
    group_by_length: bool = False,  # faster, but produces an odd training loss curve
    # wandb params
    wandb_project: str = "",
    wandb_run_name: str = "",
    wandb_watch: str = "",  # options: false | gradients | all
    wandb_log_model: str = "",  # options: false | true
    resume_from_checkpoint: str = None,  # either training checkpoint or final adapter
    prompt_template_name: str = "alpaca",
    threshold: float = 1.1,
    gama: float = 0.25,
    delta: float = 1,
    downsample: float = -1,
    seed: int = 42,
    debug: bool = False,
):
    if int(os.environ.get("LOCAL_RANK", 0)) == 0:
        print(
            f"Params using prompt template {prompt_template_name}\n"
            f"the threshold to determine the unlikelihood token: {threshold}\n"
            f"the gama weight for unlikelihood: {gama}\n"
            f"the delta weight for unlikelihood: {delta}\n"
            f"downssample rate for Hindsight-P: {downsample}\n"
            f"base_model: {base_model}\n"
            f"data_path: {data_path}\n"
            f"output_dir: {output_dir}\n"
            f"batch_size: {batch_size}\n"
            f"micro_batch_size: {micro_batch_size}\n"
            f"num_epochs: {num_epochs}\n"
            f"learning_rate: {learning_rate}\n"
            f"cutoff_len: {cutoff_len}\n"
            f"val_set_size: {val_set_size}\n"
            f"lr_scheduler: {lr_scheduler}\n"
            f"lora_r: {lora_r}\n"
            f"lora_alpha: {lora_alpha}\n"
            f"lora_dropout: {lora_dropout}\n"
            f"lora_target_modules: {lora_target_modules}\n"
            f"train_on_inputs: {train_on_inputs}\n"
            f"add_eos_token: {add_eos_token}\n"
            f"group_by_length: {group_by_length}\n"
            f"wandb_project: {wandb_project}\n"
            f"wandb_run_name: {wandb_run_name}\n"
            f"wandb_watch: {wandb_watch}\n"
            f"wandb_log_model: {wandb_log_model}\n"
            f"resume_from_checkpoint: {resume_from_checkpoint or False}\n"
            f"seed: {seed}\n"
        )
    assert (
        base_model
    ), "Please specify a --base_model, e.g. --base_model='huggyllama/llama-7b'"
    gradient_accumulation_steps = batch_size // micro_batch_size
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    prompter = Prompter(prompt_template_name)
    device_map = "auto"
    world_size = int(os.environ.get("WORLD_SIZE", 1))
    local_rank = int(os.environ.get("LOCAL_RANK", 0))
    ddp = world_size != 1
    if ddp:
        device_map = {"": int(os.environ.get("LOCAL_RANK") or 0)}
        gradient_accumulation_steps = gradient_accumulation_steps // world_size
        print("gradient_accumulation_steps: ", gradient_accumulation_steps)

    # Check if parameter passed or if set within environ
    use_wandb = len(wandb_project) > 0 or (
        "WANDB_PROJECT" in os.environ and len(os.environ["WANDB_PROJECT"]) > 0
    )
    # use_wandb = True
    # Only overwrite environ if wandb param passed
    if len(wandb_project) > 0:
        os.environ["WANDB_PROJECT"] = wandb_project
    if len(wandb_watch) > 0:
        os.environ["WANDB_WATCH"] = wandb_watch
    if len(wandb_log_model) > 0:
        os.environ["WANDB_LOG_MODEL"] = wandb_log_model


    bnb_config = BitsAndBytesConfig(
        load_in_4bit=True,
        # llm_int8_enable_fp32_cpu_offload=True,
    )
    model = NLFTLlamaForCausalLM.from_pretrained(
        base_model,
        load_in_8bit=True,
        torch_dtype=torch.float16,
        # quantization_config=bnb_config,
        # attn_implementation="flash_attention_2",
        device_map=device_map,
        threshold=threshold,
        gama=gama,
    )

    tokenizer = AutoTokenizer.from_pretrained(base_model)
    model = prepare_model_for_int8_training(model)

    config = LoraConfig(
        r=lora_r,
        lora_alpha=lora_alpha,
        target_modules=lora_target_modules,
        lora_dropout=lora_dropout,
        bias="none",
        task_type="CAUSAL_LM")

    model = get_peft_model(model, config)
    model.print_trainable_parameters()
    bos = tokenizer.bos_token_id
    eos = tokenizer.eos_token_id
    pad = tokenizer.pad_token_id
    print("pre-trained model's BOS EOS and PAD token id:", bos, eos, pad)
    tokenizer.pad_token_id = 0  # unk. we want this to be different from the eos token
    tokenizer.padding_side = "right"

    def pad_token(mode):
        if mode == 'input_ids':
            return tokenizer.pad_token_id
        elif mode == 'attention_mask':
            return 0
        elif mode == 'labels':
            return -100

    def tokenize(prompt, add_eos_token=True):
        result = tokenizer(
            prompt,
            truncation=False,
            padding=False,
            return_tensors=None,
        )
        if len(result["input_ids"]) > cutoff_len:  # truncate from left side to keep the response complete
            n_overflow = len(result["input_ids"]) - cutoff_len
            result["input_ids"] = result["input_ids"][-cutoff_len:]
            result["attention_mask"] = result["attention_mask"][-cutoff_len:]
        else:
            n_overflow = 0
        if (
            result["input_ids"][-1] != tokenizer.eos_token_id
            and add_eos_token
        ):
            result["input_ids"].append(tokenizer.eos_token_id)
            result["attention_mask"].append(1)

        result["labels"] = result["input_ids"].copy()
        result["n_overflow"] = n_overflow
        return result, n_overflow

    def generate_and_tokenize_prompt(data_point):
        instructions = data_point['instruction_list']
        tokenized_full_prompt_list = []
        for i_i, instruction in enumerate(instructions):
            data_point['instruction'] = instruction
            full_prompt = prompter.generate_prompt(
                data_point, output=True)

            tokenized_full_prompt, n_overflow_full = tokenize(full_prompt)
            if not train_on_inputs:
                user_prompt = prompter.generate_prompt(
                    data_point, output=False)

                tokenized_user_prompt, n_overflow_user = tokenize(
                    user_prompt, add_eos_token=add_eos_token)

                user_prompt_len = len(tokenized_user_prompt["input_ids"])
                offset = n_overflow_full - n_overflow_user
                user_prompt_len = user_prompt_len - offset
                if add_eos_token:
                    user_prompt_len -= 1
                if user_prompt_len > 0:
                    tokenized_full_prompt["labels"] = [-100] * user_prompt_len + tokenized_full_prompt["labels"][user_prompt_len:]  # TODO: Speed up?
                assert len(tokenized_full_prompt["labels"]) == len(tokenized_full_prompt["input_ids"])
                if i_i == 0:
                    answer_len = len(tokenized_full_prompt["labels"]) - user_prompt_len
                elif i_i == 1:
                    answer_len2 = len(tokenized_full_prompt["labels"]) - user_prompt_len
                    assert answer_len == answer_len2
                tokenized_full_prompt_list.append(tokenized_full_prompt)

        tokenized_full_prompt = tokenized_full_prompt_list[0]
        tokenized_full_prompt_base = tokenized_full_prompt_list[1]
        tokenized_full_prompt_correct = tokenized_full_prompt_list[2]
        tokenized_full_prompt_base['input_ids_wrap'] = tokenized_full_prompt['input_ids']
        tokenized_full_prompt_base['attention_mask_wrap'] = tokenized_full_prompt['attention_mask']
        tokenized_full_prompt_base['labels_wrap'] = tokenized_full_prompt['labels']
        tokenized_full_prompt_base['input_ids_correct'] = tokenized_full_prompt_correct['input_ids']
        tokenized_full_prompt_base['attention_mask_correct'] = tokenized_full_prompt_correct['attention_mask']
        tokenized_full_prompt_base['labels_correct'] = tokenized_full_prompt_correct['labels']
        return tokenized_full_prompt_base

    if data_path.endswith(".json") or data_path.endswith(".jsonl"):
        data = read_json(data_path) if data_path.endswith(".json") else read_jsonl(data_path)
    else:
        raise NotImplementedError

    if resume_from_checkpoint:
        # Check the available weights and load them
        checkpoint_name = os.path.join(
            resume_from_checkpoint, "pytorch_model.bin"
        )  # Full checkpoint
        if not os.path.exists(checkpoint_name):
            checkpoint_name = os.path.join(
                resume_from_checkpoint, "adapter_model.bin"
            )  # only LoRA model - LoRA config above has to fit
            resume_from_checkpoint = (
                False  # So the trainer won't try loading its state
            )
        # The two files above have a different name depending on how they were saved, but are actually the same.
        if os.path.exists(checkpoint_name):
            print(f"Restarting from {checkpoint_name}")
            adapters_weights = torch.load(checkpoint_name)
            set_peft_model_state_dict(model, adapters_weights)
        else:
            print(f"Checkpoint {checkpoint_name} not found")

    file_name = os.path.join("templates", f"{prompt_template_name}.json")
    with open(file_name) as fp:
        template = json.load(fp)

    if "llama3" in base_model or "Llama-3" in base_model:
        SYSTEM_PROMPT = [
            {"role": "system", "content": "{instruction}"},
            {"role": "user", "content": "{input}"}
        ]

        SYSTEM_PROMPT_JUDGE = [
            {"role": "system",
             "content": "Write a bad response to the instruction. Your response should contain the weaknesses mentioned in the below judgment.\n\n### Instruction:\n{instruction}\n\n"},
            {"role": "user", "content": "### Question:\n{input}\n\n### Judgment:\n{judgment}"}
        ]

        SYSTEM_PROMPT_CORRECT = [
            {"role": "system",
             "content": "Assuming you are a math expert, the question and the standard answer are as follows. Please refer to the standard answer and give your answer.\n\n### Instruction:\n{instruction}\n\n"},
            {"role": "user", "content": "### Question:\n{input}\n\n### Standard answer:\n{answer}"}
        ]

        template["prompt_no_input_judge"] = tokenizer.apply_chat_template(SYSTEM_PROMPT_JUDGE, tokenize=False,
                                                                          add_generation_prompt=True).replace(
            "<|begin_of_text|>", "")
        template["prompt_no_input_correct"] = tokenizer.apply_chat_template(SYSTEM_PROMPT_CORRECT, tokenize=False,
                                                                          add_generation_prompt=True).replace(
            "<|begin_of_text|>", "")

        template["prompt_no_input"] = tokenizer.apply_chat_template(SYSTEM_PROMPT, tokenize=False,
                                                                    add_generation_prompt=True).replace(
            "<|begin_of_text|>", "")
    print(template["prompt_no_input"])
    train_processed = []
    n_pos = 0
    n_neg = 0
    pos_ids = []
    neg_ids = []
    for ix, x in enumerate(data):
        x_judgment = x['judgment']
        x_score = x['score'] if 'score' in x else None
        if x_score is not None and x_score >= 7:
            x_judgment = None
        x_input = x['input']
        x_instruction = x['instruction']
        x_out = x['output']
        x_i_ans = x['i_ans'] if "i_ans" in x else None
        x_answer = x['answer']
        # if x_input:
        #     x_instruction = f"{x_instruction}\n{x_input}"
        if x_judgment is not None:
            hindsight_n = template["prompt_no_input_judge"].format(judgment=x_judgment, instruction=x_instruction, input=x_input)
            correct = template["prompt_no_input_correct"].format(answer=x_answer, instruction=x_instruction, input=x_input)
            base = template["prompt_no_input"].format(instruction=x_instruction, input=x_input)
            x_new = {
                'output': x_out,
                'input': None,
                'instruction_list': [hindsight_n, base,correct],
                'i_ans': x_i_ans,
                'score': x_score,
                "polarity": 0,
            }
            n_neg += 1
            neg_ids.append(len(train_processed))
            train_processed.append(x_new)
        else:
            base = template["prompt_no_input"].format(instruction=x_instruction, input=x_input)
            correct = template["prompt_no_input_correct"].format(answer=x_answer, instruction=x_instruction, input=x_input)
            x_new = {
                'output': x_out,
                'input': None,
                'instruction_list': [base, base,correct],
                'i_ans': x_i_ans,
                'score': x_score,
                "polarity": 1,
            }
            n_pos += 1
            pos_ids.append(len(train_processed))
            train_processed.append(x_new)
    print(f"n_pos:{n_pos}, n_neg:{n_neg}")
    if downsample != -1:
        n_keep = int(n_neg * downsample)
        if n_keep < n_pos:
            pos_keep_ids = random.sample(pos_ids, n_keep)
            pos_ids = sorted(pos_ids, reverse=True)
            for idx in pos_ids:
                if idx not in pos_keep_ids:
                    train_processed.pop(idx)
    print(f"after downsampling, the total num of train data is: {len(train_processed)}")
    train_processed = Dataset.from_list(train_processed)
    print(f"num of training data: {len(train_processed)}")
    train_data = train_processed.map(generate_and_tokenize_prompt)
    val_data = None


    if not ddp and torch.cuda.device_count() > 1:
        # keeps Trainer from trying its own DataParallelism when more than 1 gpu is available
        model.is_parallelizable = True
        model.model_parallel = True

    trainer = transformers.Trainer(
        model=model,
        train_dataset=train_data,
        eval_dataset=val_data,
        args=transformers.TrainingArguments(
            per_device_train_batch_size=micro_batch_size,
            gradient_accumulation_steps=gradient_accumulation_steps,
            warmup_ratio=0.1,
            num_train_epochs=num_epochs,
            learning_rate=learning_rate,
            # dataloader_num_workers=16,
            # fp16=True,
            bf16=True if not debug else False,
            logging_steps=1,
            optim="adamw_torch",
            evaluation_strategy="steps" if val_set_size > 0 else "no",
            save_strategy="steps",
            eval_steps=200 if val_set_size > 0 else None,
            save_steps=1000,
            lr_scheduler_type=lr_scheduler,
            output_dir=output_dir,
            save_total_limit=2,
            load_best_model_at_end=True if val_set_size > 0 else False,
            ddp_find_unused_parameters=False if ddp else None,
            group_by_length=group_by_length,
            report_to="wandb" if use_wandb else None,
            run_name=wandb_run_name if use_wandb else None,
        ),
        data_collator=MyDataCollator(
            tokenizer, pad_to_multiple_of=8, return_tensors="pt", padding="max_length"
        ),
        callbacks=[
            SavePeftModelOnEpochEndCallback(output_dir=output_dir),
        ],
    )
    model.config.use_cache = False

    if torch.__version__ >= "2" and sys.platform != "win32":
        model = torch.compile(model)

    trainer.train(resume_from_checkpoint=resume_from_checkpoint)
    if local_rank == 0:
        model.save_pretrained(output_dir)
        # model.base_model.save_pretrained(output_dir)
        pytorch_model_path = os.path.join(output_dir, "pytorch_model.bin")
        torch.save({}, pytorch_model_path)
        tokenizer.save_pretrained(output_dir)


if __name__ == "__main__":
    torch.cuda.empty_cache()
    fire.Fire(train)
