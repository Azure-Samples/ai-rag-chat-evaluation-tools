import json
import logging
import math
import random
from collections.abc import Generator
from pathlib import Path

from azure.search.documents import SearchClient

from evaltools import service_setup

logger = logging.getLogger("evaltools")


def generate_test_qa_data(
    openai_config: dict,
    num_questions_total: int,
    num_questions_per_source: int,
    output_file: Path,
    source_retriever: Generator[dict, None, None],
    source_to_text: callable,
    answer_formatter: callable,
):
    try:
        from azure.ai.generative.synthetic.qa import QADataGenerator, QAType
    except ImportError:
        logger.error(
            "Azure AI Generative package is deprecated and no longer working, so this functionality is disabled."
        )

    logger.info(
        "Generating %d questions total, %d per source, based on search results",
        num_questions_total,
        num_questions_per_source,
    )
    qa_generator = QADataGenerator(model_config=openai_config)

    qa: list[dict] = []
    for source in source_retriever():
        if len(qa) > num_questions_total:
            logger.info("Generated enough questions already, stopping")
            break
        result = qa_generator.generate(
            text=source_to_text(source),
            qa_type=QAType.LONG_ANSWER,
            num_questions=num_questions_per_source,
        )

        for question, answer in result["question_answers"]:
            qa.append({"question": question, "truth": answer_formatter(answer, source)})

    logger.info("Writing %d questions to %s", len(qa), output_file)
    directory = Path(output_file).parent
    if not directory.exists():
        directory.mkdir(parents=True)
    with open(output_file, "w", encoding="utf-8") as f:
        for item in qa[0:num_questions_total]:
            f.write(json.dumps(item) + "\n")


def generate_test_qa_data_for_search_index(
    openai_config: dict,
    num_questions_total: int,
    num_questions_per_source: int,
    output_file: Path,
    search_client: SearchClient,
    citation_field_name: str,
):
    def source_retriever() -> Generator[dict, None, None]:
        for doc in search_client.search("", top=1000):
            logger.info("Processing search document %s", doc[citation_field_name])
            yield doc

    def source_to_text(source) -> str:
        return source["content"]

    def answer_formatter(answer, source) -> str:
        return f"{answer} [{source[citation_field_name]}]"

    generate_test_qa_data(
        openai_config,
        num_questions_total,
        num_questions_per_source,
        output_file,
        source_retriever,
        source_to_text,
        answer_formatter,
    )


def generate_based_on_questions(openai_client, model: str, qa: list, num_questions: int, prompt: str):
    existing_questions = ""
    if qa:
        qa = random.sample(qa, len(qa))  # Shuffle questions for some randomness
        existing_questions = "\n".join([item["question"] for item in qa])

    gpt_response = openai_client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "user",
                "content": f"{prompt} Only generate {num_questions} TOTAL. Separate each question by a new line. \n{existing_questions}",  # noqa: E501
            }
        ],
        n=1,
        max_tokens=num_questions * 50,
        temperature=0.3,
    )

    qa = []
    for message in gpt_response.choices[0].message.content.split("\n")[0:num_questions]:
        qa.append({"question": message, "truth": f"Generated from this prompt: {prompt}"})
    return qa


def generate_dontknows_qa_data(openai_config: dict, num_questions_total: int, input_file: Path, output_file: Path):
    logger.info("Generating off-topic questions based on %s", input_file)
    with open(input_file, encoding="utf-8") as f:
        qa = [json.loads(line) for line in f.readlines()]

    openai_client = service_setup.get_openai_client(openai_config)
    dontknows_qa = []
    num_questions_each = math.ceil(num_questions_total / 4)
    dontknows_qa += generate_based_on_questions(
        openai_client,
        openai_config.model,
        qa,
        num_questions_each,
        f"Given these questions, suggest {num_questions_each} questions that are very related but are not directly answerable by the same sources. Do not simply ask for other examples of the same thing - your question should be standalone.",  # noqa: E501
    )
    dontknows_qa += generate_based_on_questions(
        openai_client,
        openai_config.model,
        qa,
        num_questions_each,
        f"Given these questions, suggest {num_questions_each} questions with similar keywords that are about publicly known facts.",  # noqa: E501
    )
    dontknows_qa += generate_based_on_questions(
        openai_client,
        openai_config.model,
        qa,
        num_questions_each,
        f"Given these questions, suggest {num_questions_each} questions that are not related to these topics at all but have well known answers.",  # noqa: E501
    )
    remaining = num_questions_total - len(dontknows_qa)
    dontknows_qa += generate_based_on_questions(
        openai_client,
        openai_config.model,
        qa=None,
        num_questions=remaining,
        prompt=f"Suggest {remaining} questions that are nonsensical, and would result in confusion if you asked it.",  # noqa: E501
    )

    logger.info("Writing %d off-topic questions to %s", len(dontknows_qa), output_file)
    directory = Path(output_file).parent
    if not directory.exists():
        directory.mkdir(parents=True)
    with open(output_file, "w", encoding="utf-8") as f:
        for item in dontknows_qa:
            f.write(json.dumps(item) + "\n")
