import asyncio

from dotenv import load_dotenv
from common.utils.helper import Helper
from common.utils.settings import aws_client, AWS_REGION, MODEL_ID

load_dotenv()


class CohereReranker:
    
    def __init__(self):
        self.client = aws_client('bedrock-agent-runtime')
        self.model_id = Helper.get_property("model_id_cohere_reranker", default="cohere.rerank-v3-5:0")
        self.region = AWS_REGION
        self.model_package_arn = f"arn:aws:bedrock:{self.region}::foundation-model/{self.model_id}"

    async def rerank_results(self, query, documents, top_k: int = 5):

        if not documents:
            return []
        
        source = []
        for doc in documents:
            if isinstance(doc, str):
                source.append({
                        "type": "INLINE",
                        "inlineDocumentSource": {
                            "type": "TEXT",
                            "textDocument": {
                                "text": doc
                            }
                        }
                    })
            elif isinstance(doc, dict) and 'text' in doc:
                source.append({
                        "type": "INLINE",
                        "inlineDocumentSource": {
                            "type": "TEXT",
                            "textDocument": {
                                "text": doc['text']
                            }
                        }
                    })
            else:
                raise ValueError("Each document must be either a string or a dict with a 'text' key.")
        

        num_results = min(top_k, len(source))

        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None,
            self.invoke_rerank_sync,
            query,
            source,
            num_results
            )

        results = response['results']
    


        reranked = []
        for result in results:
            idx = result['index']
            score = result['relevanceScore']
            original_doc = documents[idx]
            doc_dict = original_doc if isinstance(original_doc, dict) else {"text": original_doc}
            reranked.append({
                **doc_dict,
                "rerank_score": score,
                "original_index": idx
            })
            
        return reranked

    def invoke_rerank_sync(self, query, sources, num_results):

        return self.client.rerank(
                        queries=[
                            {
                                "type": "TEXT",
                                "textQuery": {
                                    "text": query
                                }
                            }
                        ],
                        sources=sources,
                        rerankingConfiguration={
                            "type": "BEDROCK_RERANKING_MODEL",
                            "bedrockRerankingConfiguration": {
                            "numberOfResults": num_results,
                            "modelConfiguration": {
                            "modelArn": self.model_package_arn,
                                }
                            }
                        }
        )




