# ChatGSE LLM connectivity
# connect to API
# keep track of message history
# query API
# correct response
# update usage stats

import streamlit as st

ss = st.session_state

from abc import ABC, abstractmethod
import openai

from langchain.chat_models import ChatOpenAI
from langchain.schema import AIMessage, HumanMessage, SystemMessage
from langchain.llms import HuggingFaceHub

import nltk
import json

from ._stats import get_stats

OPENAI_MODELS = [
    "gpt-3.5-turbo",
    "gpt-3.5-turbo-16k",
    "gpt-3.5-turbo-0301",  # legacy 3.5-turbo, until Sep 13, 2023
    "gpt-3.5-turbo-0613",  # updated 3.5-turbo
    "gpt-4",
]

HUGGINGFACE_MODELS = ["bigscience/bloom"]

TOKEN_LIMITS = {
    "gpt-3.5-turbo": 4000,
    "gpt-3.5-turbo-16k": 16000,
    "gpt-3.5-turbo-0301": 4000,
    "gpt-3.5-turbo-0613": 4000,
    "gpt-4": 8000,
    "bigscience/bloom": 1000,
}


class Conversation(ABC):
    """

    Use this class to set up a connection to an LLM API. Can be used to set the
    user name and API key, append specific messages for system, user, and AI
    roles (if available), set up the general context as well as manual and
    tool-based data inputs, and finally to query the API with prompts made by
    the user.

    The conversation class is expected to have a `messages` attribute to store
    the conversation, and a `history` attribute, which is a list of messages in
    a specific format for logging / printing.

    """

    def __init__(
        self,
        model_name: str,
        prompts: dict,
        split_correction: bool = False,
    ):
        super().__init__()
        self.model_name = model_name
        self.prompts = prompts
        self.split_correction = split_correction
        self.history = []
        self.messages = []
        self.ca_messages = []

    def set_user_name(self, user_name: str):
        self.user_name = user_name

    @abstractmethod
    def set_api_key(self, api_key: str):
        pass

    def get_prompts(self):
        return self.prompts

    def set_prompts(self, prompts: dict):
        self.prompts = prompts

    def append_ai_message(self, message: str):
        self.messages.append(
            AIMessage(
                content=message,
            ),
        )

    def append_system_message(self, message: str):
        self.messages.append(
            SystemMessage(
                content=message,
            ),
        )

    def append_user_message(self, message: str):
        self.messages.append(
            HumanMessage(
                content=message,
            ),
        )

    def setup(self, context: str):
        """
        Set up the conversation with general prompts and a context.
        """
        for msg in self.prompts["primary_model_prompts"]:
            if msg:
                self.messages.append(
                    SystemMessage(
                        content=msg,
                    ),
                )

        for msg in self.prompts["correcting_agent_prompts"]:
            if msg:
                self.ca_messages.append(
                    SystemMessage(
                        content=msg,
                    ),
                )

        self.context = context
        msg = f"The topic of the research is {context}."
        self.append_system_message(msg)

    def setup_data_input_manual(self, data_input: str):
        self.data_input = data_input
        msg = f"The user has given information on the data input: {data_input}."
        self.append_system_message(msg)

    def setup_data_input_tool(self, df, input_file_name: str):
        self.data_input_tool = df

        for tool_name in self.prompts["tool_prompts"]:
            if tool_name in input_file_name:
                msg = self.prompts["tool_prompts"][tool_name].format(df=df)
                self.append_system_message(msg)

    def query(self, text: str):
        self.append_user_message(text)

        if ss.get("docsum"):
            if ss.docsum.use_prompt:
                self._inject_context(text)

        msg, token_usage = self._primary_query()

        if not token_usage:
            # indicates error
            return (msg, token_usage, None)

        cor_msg = (
            "Correcting (using single sentences) ..."
            if self.split_correction
            else "Correcting ..."
        )

        with st.spinner(cor_msg):
            corrections = []
            if self.split_correction:
                nltk.download("punkt")
                tokenizer = nltk.data.load("tokenizers/punkt/english.pickle")
                sentences = tokenizer.tokenize(msg)
                for sentence in sentences:
                    correction = self._correct_response(sentence)

                    if not str(correction).lower() in ["ok", "ok."]:
                        corrections.append(correction)
            else:
                correction = self._correct_response(msg)

                if not str(correction).lower() in ["ok", "ok."]:
                    corrections.append(correction)

        if not corrections:
            return (msg, token_usage, None)

        correction = "\n".join(corrections)
        return (msg, token_usage, correction)

    @abstractmethod
    def _primary_query(self, text: str):
        pass

    @abstractmethod
    def _correct_response(self, msg: str):
        pass

    def _inject_context(self, text: str):
        if not ss.docsum.used:
            st.info(
                "No document has been analysed yet. To use document "
                "summarisation, please analyse at least one document first."
            )
            return

        sim_msg = (
            f"Performing similarity search to inject {ss.docsum.n_results}"
            " fragments ..."
        )

        with st.spinner(sim_msg):
            statements = [
                doc.page_content
                for doc in ss.docsum.similarity_search(
                    text,
                    ss.docsum.n_results,
                )
            ]
        prompts = self.prompts["docsum_prompts"]
        if statements:
            ss.current_statements = statements
            for i, prompt in enumerate(prompts):
                if i == len(prompts) - 1:
                    self.append_system_message(
                        prompt.format(statements=statements)
                    )
                else:
                    self.append_system_message(prompt)

    def get_msg_json(self):
        """
        Return a JSON representation (of a list of dicts) of the messages in
        the conversation. The keys of the dicts are the roles, the values are
        the messages.
        """
        d = []
        for msg in self.messages:
            if isinstance(msg, SystemMessage):
                role = "system"
            elif isinstance(msg, HumanMessage):
                role = "user"
            elif isinstance(msg, AIMessage):
                role = "ai"
            else:
                raise ValueError(f"Unknown message type: {type(msg)}")

            d.append({role: msg.content})

        return json.dumps(d)


class GptConversation(Conversation):
    def __init__(
        self,
        model_name: str,
        prompts: dict,
        split_correction: bool,
    ):
        """
        Connect to OpenAI's GPT API and set up a conversation with the user.
        Also initialise a second conversational agent to provide corrections to
        the model output, if necessary.
        """
        super().__init__(
            model_name=model_name,
            prompts=prompts,
            split_correction=split_correction,
        )

        self.ca_model_name = "gpt-3.5-turbo"
        # TODO make accessible by drop-down

    def set_api_key(self, api_key: str, user: str):
        """
        Set the API key for the OpenAI API. If the key is valid, initialise the
        conversational agent. Set the user for usage statistics.
        """
        openai.api_key = api_key
        self.user = user

        try:
            openai.Model.list()
            self.chat = ChatOpenAI(
                model_name=self.model_name,
                temperature=0,
                openai_api_key=api_key,
            )
            self.ca_chat = ChatOpenAI(
                model_name=self.ca_model_name,
                temperature=0,
                openai_api_key=api_key,
            )
            if user == "community":
                self.usage_stats = get_stats(user=user)
            ss.openai_api_key = api_key
            return True
        except openai.error.AuthenticationError as e:
            return False

    def _primary_query(self):
        try:
            response = self.chat.generate([self.messages])
        except (
            openai.error.InvalidRequestError,
            openai.error.APIConnectionError,
            openai.error.RateLimitError,
            openai.error.APIError,
        ) as e:
            return str(e), None

        msg = response.generations[0][0].text
        token_usage = response.llm_output.get("token_usage")

        self._update_usage_stats(self.model_name, token_usage)

        self.append_ai_message(msg)

        return msg, token_usage

    def _correct_response(self, msg: str):
        ca_messages = self.ca_messages.copy()
        ca_messages.append(
            HumanMessage(
                content=msg,
            ),
        )
        ca_messages.append(
            SystemMessage(
                content="If there is nothing to correct, please respond "
                "with just 'OK', and nothing else!",
            ),
        )

        response = self.ca_chat.generate([ca_messages])

        correction = response.generations[0][0].text
        token_usage = response.llm_output.get("token_usage")

        self._update_usage_stats(self.ca_model_name, token_usage)

        return correction

    def _update_usage_stats(self, model: str, token_usage: dict):
        """
        Update redis database with token usage statistics using the usage_stats
        object with the increment method.
        """
        if self.user == "community":
            self.usage_stats.increment(
                f"usage:[date]:[user]",
                {f"{k}:{model}": v for k, v in token_usage.items()},
            )


class BloomConversation(Conversation):
    def __init__(
        self,
        model_name: str,
        prompts: dict,
        split_correction: bool,
    ):
        super().__init__(
            model_name=model_name,
            prompts=prompts,
            split_correction=split_correction,
        )

        self.messages = []

    def set_api_key(self, api_key: str, user: str):
        self.chat = HuggingFaceHub(
            repo_id=self.model_name,
            model_kwargs={"temperature": 1.0},  # "regular sampling"
            # as per https://huggingface.co/docs/api-inference/detailed_parameters
            huggingfacehub_api_token=api_key,
        )

        try:
            self.chat.generate(["Hello, I am a biomedical researcher."])
            return True
        except ValueError as e:
            return False

    def _cast_messages(self, messages):
        """
        Render the different roles of the chat-based conversation as plain text.
        """
        cast = ""
        for m in messages:
            if isinstance(m, SystemMessage):
                cast += f"System: {m.content}\n"
            elif isinstance(m, HumanMessage):
                cast += f"Human: {m.content}\n"
            elif isinstance(m, AIMessage):
                cast += f"AI: {m.content}\n"
            else:
                raise ValueError(f"Unknown message type: {type(m)}")

        return cast

    def _primary_query(self):
        response = self.chat.generate([self._cast_messages(self.messages)])

        msg = response.generations[0][0].text
        token_usage = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }

        self.append_ai_message(msg)

        return msg, token_usage

    def _correct_response(self, msg: str):
        return "ok"
