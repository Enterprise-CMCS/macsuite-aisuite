import json 
import asyncio
import boto3
import os
import numpy as np

from common.utils.settings import aws_client
from common.utils.settings import MODEL_ID, EMBEDDING_DIMENSION


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
        
        # Validate text is not empty
        if not text or not str(text).strip():
            raise ValueError("Cannot generate embedding for empty text")

        request_body = {
                "texts": [text],
                "input_type": "search_document"
            }
        
        try:
            response = self.client.invoke_model(
                modelId=self.model_id,
                contentType='application/json',
                accept='application/json',
                body=json.dumps(request_body)
            )
        except Exception as e:
            print(f"Error invoking model '{self.model_id}'")
            print(f"Text preview: '{text[:100]}...'")
            print(f"Request body: {json.dumps(request_body)}")
            raise

        response_body = json.loads(response['body'].read())
        
        # Parse response based on model type - Cohere Embed v4 returns {"embeddings": {"float": [[embedding_values]]}, ...}
        if 'embeddings' in response_body:
            embeddings_obj = response_body['embeddings']
            
            # Check if it's a dict with 'float' key
            if isinstance(embeddings_obj, dict) and 'float' in embeddings_obj:
                embeddings_list = embeddings_obj['float']
                if isinstance(embeddings_list, list) and len(embeddings_list) > 0:
                    float_array = embeddings_list[0]  # Get the first embedding
                    return np.array(float_array, dtype=np.float32)
                return np.array(embeddings_list, dtype=np.float32)
            # Or it's directly a list
            elif isinstance(embeddings_obj, list) and len(embeddings_obj) > 0:
                first_item = embeddings_obj[0]
                if isinstance(first_item, list):
                    return np.array(first_item, dtype=np.float32)
                return np.array(embeddings_obj, dtype=np.float32)
            else:
                return np.array(embeddings_obj, dtype=np.float32)
        elif 'float' in response_body:
            # Legacy format handling
            embeddings = response_body['float']
            if isinstance(embeddings, list) and len(embeddings) > 0:
                float_array = embeddings[0]
                return np.array(float_array, dtype=np.float32)
            return np.array(embeddings, dtype=np.float32)
        elif 'embedding' in response_body:
            return np.array(response_body['embedding'], dtype=np.float32)
        else:  
            raise ValueError(f"Unexpected response format. Keys: {response_body.keys()}")
        
    async def batch_get_embeddings(self, texts, max_concurrent_requests=10):

        semaphore = asyncio.Semaphore(max_concurrent_requests)

        async def bounded_embedding(text):
            async with semaphore:
                return await self.get_embedding(text)
            
        results = await asyncio.gather(*(bounded_embedding(text) for text in texts), return_exceptions=True)
        return results
    
    