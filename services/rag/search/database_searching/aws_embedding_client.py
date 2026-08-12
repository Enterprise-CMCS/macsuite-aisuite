import json 
import asyncio
import boto3
import os

from common.utils.embedding_input_types import (
    INPUT_TYPE_QUERY,
    build_embedding_request,
)
from common.utils.settings import aws_client
from common.utils.settings import (MODEL_ID, EMBEDDING_DIMENSION)


class BedrockEmbeddingClient:

    def __init__(self):
        self.client = aws_client('bedrock-runtime')
        self.model_id = MODEL_ID
        self.dimension = EMBEDDING_DIMENSION

    async def get_embedding(self, text):
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            self.invoke_model_sync,
            text
            )
        return response
    
    def invoke_model_sync(self, text):
        

        request_body = build_embedding_request([text], INPUT_TYPE_QUERY)
        
        try:
            response = self.client.invoke_model(
                modelId=self.model_id,
                contentType='application/json',
                accept='application/json',
                body=json.dumps(request_body)
            )
        except Exception as e:
            print(f"Error invoking model '{self.model_id}'")
            print(f"Request body: {json.dumps(request_body)}")
            raise

        response_body = json.loads(response['body'].read())
        
        
        if 'embeddings' in response_body:
            embeddings = response_body['embeddings']

            if isinstance(embeddings, list) and len(embeddings) > 0:
                return embeddings[0] if isinstance(embeddings[0], list) else embeddings
            return embeddings
        elif 'embedding' in response_body:
            return response_body['embedding']
        else:  
            raise ValueError(f"Unexpected response format: {response_body}")
        
    async def batch_get_embeddings(self, texts, max_concurrent_requests=1):

        semaphore = asyncio.Semaphore(max_concurrent_requests)

        async def bounded_embedding(text):
            async with semaphore:
                return await self.get_embedding(text)
            
        results = await asyncio.gather(*(bounded_embedding(text) for text in texts), return_exceptions=True)
        return results
    
    