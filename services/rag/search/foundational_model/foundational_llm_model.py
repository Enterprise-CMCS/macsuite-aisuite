from search.database_searching.results import get_search_results
import json
import asyncio
import os
import boto3
from functools import partial
from concurrent.futures import ThreadPoolExecutor
from common.utils.settings import aws_client
from common.utils.helper import Helper

# Initialize environment to load settings
from common.utils.settings import init_env
init_env()


aws_region = Helper.get_property("aws_region")
model_id_key = Helper.get_property("foundation_llm_model_id")


system_prompts = {
    "text": """You are a helpful AI assistant that answers questions based on provided context. 
    Your goal is to provide accurate, concise, and well-structured answers to user queries using only the information from the search results.

    If the search results don't contain enough information to answer the question, clearly state that."""
}

prompt_templates = {
    "text": """User Question: {query}

    Search Results:
    {text_content}

Please provide a clear and concise answer to the user's question based on the search results above. Structure your response in a helpful way."""
}

def _extract_text_with_foundational_model_sync(text_content: str, search_type: str, query: str, prompt_strategy: str = "text") -> dict:
    
    system_prompt = system_prompts.get(prompt_strategy, system_prompts["text"])
    user_prompt_text = prompt_templates.get(prompt_strategy, prompt_templates["text"]).format(
        query=query,
        text_content=text_content
    )
    

    request_body = {
        "schemaVersion": "messages-v1",
        "system": [
            {
                "text": system_prompt
            }
        ],
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "text": user_prompt_text
                    }
                ]
            }
        ],
        "inferenceConfig": {
            "maxTokens": 512
        }
    }
    
    client = aws_client('bedrock-runtime')
    response = client.invoke_model(
        modelId=model_id_key,
        body=json.dumps(request_body)
    )
    
    response_body = json.loads(response['body'].read())
    result = response_body['output']['message']['content'][0]['text']

    return {
        "search_type": search_type,
        "response": result,
        "strategy": prompt_strategy
    }


async def extract_text_with_foundational_model_async(text_content: str, search_type: str, query: str, prompt_strategy: str = "text") -> dict:

    loop = asyncio.get_event_loop()
    with ThreadPoolExecutor() as executor:
        func = partial(_extract_text_with_foundational_model_sync, text_content, search_type, query, prompt_strategy)
        return await loop.run_in_executor(executor, func)



async def process_query_with_foundational_model(
    query: str,
    contract_id: str | None = None,
) -> dict:

    print(f"Processing query: {query}")
    


    search_results = await get_search_results(query, contract_id)

    print("Search results obtained. Extracting answers with AWS Nova Pro...")
    
    #bm25_task = extract_text_with_foundational_model_async(search_results['bm25'], 'bm25', query)
    semantic_task = extract_text_with_foundational_model_async(search_results['semantic'], 'semantic', query)
    # hybrid_task = extract_text_with_foundational_model_async(search_results['hybrid'], 'hybrid', query)
    # reranked_task = extract_text_with_foundational_model_async(search_results['reranked'], 'reranked', query)
    

    semantic_response = await semantic_task
    
    return {
        
        "query": query,
        #"bm25": bm25_response,
        "semantic": semantic_response,
    }