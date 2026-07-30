#!/usr/bin/env python3

import sys
import json
import asyncio
import re
from pathlib import Path
from typing import Optional

import pandas as pd

src_path = Path(__file__).parent.parent.parent
sys.path.insert(0, str(src_path))

from search.database_searching.agents import search_agent, ChatDeps
from common.utils.logger import get_logger

logger = get_logger(__name__)

HEADER_ROW_INDEX = 10
DATA_START_INDEX = 11
REQUIREMENT_COL = "Requirement"
RECOMMENDATION_COL = "Recommendation"
RAG_RESPONSE_COL = "RAG Response"
SOURCE_COL = "Source"


class ExcelRAGProcessor:

    def __init__(self, excel_file_path: str):
        self.excel_file_path = Path(excel_file_path)
        self.df: Optional[pd.DataFrame] = None
        self.processed_count = 0
        self.error_count = 0
        logger.info(f"Initialized ExcelRAGProcessor for: {self.excel_file_path}")

    def load_excel(self):

        if not self.excel_file_path.exists():
            raise FileNotFoundError(f"Excel file not found: {self.excel_file_path}")

        logger.info(f"Loading Excel file: {self.excel_file_path}")
        self.df = pd.read_excel(
            self.excel_file_path,
            header=HEADER_ROW_INDEX,
            engine="openpyxl",
        )
        logger.info(f"Loaded dataframe with {len(self.df)} data rows, columns: {list(self.df.columns)}")

        for col in [RECOMMENDATION_COL, RAG_RESPONSE_COL, SOURCE_COL]:
            self.df[col] = pd.Series([pd.NA] * len(self.df), dtype="object", index=self.df.index)

        return self.df

    async def process_requirement(self, requirement: str, retry_unclear: bool = True) -> dict:
        try:
            logger.info(f"Processing: {requirement[:80]}...")

            deps = ChatDeps(acronyms={})
            agent_response = await search_agent.run(user_prompt=requirement, deps=deps)

            if hasattr(agent_response, 'output'):
                response_text = agent_response.output
            else:
                response_text = str(agent_response)

            logger.info(f"Agent raw output (first 200 chars): {response_text[:200]}...")

            parsed = self._parse_agent_response(response_text)
            recommendation = parsed.get('recommendation', 'UNCLEAR')

            if recommendation == 'UNCLEAR' and retry_unclear:
                logger.info("Result is UNCLEAR, retrying with more specific prompt...")
                retry_prompt = (
                    "Based on the available documentation, please determine if the following "
                    "requirement is MET or NOT MET. If there is insufficient information, "
                    "explain what specific information is missing.\n\n"
                    f"Requirement: {requirement}\n\n"
                    "Please provide a clear MET or NOT MET determination with specific evidence, "
                    "or explain exactly what information is needed."
                )

                retry_response = await search_agent.run(user_prompt=retry_prompt, deps=deps)
                if hasattr(retry_response, 'output'):
                    retry_text = retry_response.output
                else:
                    retry_text = str(retry_response)

                parsed = self._parse_agent_response(retry_text)
                recommendation = parsed.get('recommendation', 'UNCLEAR')
                logger.info(f"Retry result - Recommendation: {recommendation}")

            logger.info(f"[OK] Processed successfully - Recommendation: {recommendation}")

            return {
                "recommendation": parsed.get('recommendation', 'UNCLEAR'),
                "response": parsed.get('response', 'No response'),
                "source": parsed.get('source', 'N/A'),
            }

        except Exception as e:
            logger.error(f"Error processing requirement: {str(e)}")
            self.error_count += 1
            return {
                "recommendation": "ERROR",
                "response": f"Error processing requirement: {str(e)}",
                "source": "N/A",
            }

    def _parse_agent_response(self, response_text: str) -> dict:
        try:
            response_text = re.sub(r'<thinking>.*?</thinking>', '', response_text, flags=re.DOTALL)
            response_text = response_text.strip()

            json_match = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', response_text, re.DOTALL)

            if json_match:
                json_str = json_match.group()
                try:
                    result = json.loads(json_str)
                except json.JSONDecodeError:
                    result = {
                        "Response": response_text[:500],
                        "Recommendation": "UNCLEAR",
                        "Source": "N/A",
                    }
            else:
                result = {
                    "Response": response_text[:500],
                    "Recommendation": "UNCLEAR",
                    "Source": "N/A",
                }

            recommendation = result.get('Recommendation', result.get('Verdict', 'UNCLEAR'))
            response = result.get('Response', result.get('Reasoning', response_text[:500]))
            source = result.get('Source', 'N/A')
            page = result.get('Page', '')

            source_with_page = f"{source}: {page}" if page and page != 'N/A' else source

            return {
                "recommendation": recommendation.upper() if recommendation else 'UNCLEAR',
                "response": response if response else 'No response provided',
                "source": source_with_page,
            }

        except Exception as e:
            logger.error(f"Error parsing response: {str(e)}")
            return {
                "recommendation": "UNCLEAR",
                "response": response_text[:500] if response_text else "Error parsing response",
                "source": "N/A",
            }

    async def process_all(self, max_rows: Optional[int] = None):
        if self.df is None:
            raise RuntimeError("Call load_excel() first")

        requirement_rows = self.df[self.df[REQUIREMENT_COL].notna()].index.tolist()

        if max_rows:
            requirement_rows = requirement_rows[:max_rows]

        total = len(requirement_rows)
        logger.info(f"Processing {total} requirements...")

        for i, idx in enumerate(requirement_rows):
            requirement = str(self.df.at[idx, REQUIREMENT_COL]).strip()
            if not requirement:
                continue

            logger.info(f"\n[{i+1}/{total}] Row {idx}: {requirement[:100]}...")

            result = await self.process_requirement(requirement)

            self.df.at[idx, RECOMMENDATION_COL] = result['recommendation']
            self.df.at[idx, RAG_RESPONSE_COL] = result['response']
            self.df.at[idx, SOURCE_COL] = result['source']

            self.processed_count += 1
            logger.info(f"Row {idx} updated | Rec='{result['recommendation']}' | Total: {self.processed_count}/{total}")

            if self.processed_count % 5 == 0:
                self.save_progress()

    def save_progress(self):
        try:
            output_file = self.excel_file_path.parent / "rag_results.xlsx"
            self._save_to_excel(output_file)
            logger.info(f"[SAVED] Progress saved to: {output_file}")
        except Exception as e:
            logger.error(f"Error saving progress: {str(e)}")

    def save_final(self, output_path: Optional[Path] = None):
        if output_path is None:
            output_path = self.excel_file_path.parent / "rag_results.xlsx"

        self._save_to_excel(output_path)
        logger.info(f"\n{'='*80}")
        logger.info(f"[SUCCESS] FINAL EXCEL SAVED: {output_path}")
        logger.info(f"{'='*80}")
        logger.info(f"Total requirements processed: {self.processed_count}")
        logger.info(f"Errors encountered: {self.error_count}")

    def _save_to_excel(self, output_path: Path):
        header_df = pd.read_excel(
            self.excel_file_path,
            header=None,
            nrows=HEADER_ROW_INDEX,
            engine="openpyxl",
        )

        with pd.ExcelWriter(str(output_path), engine="openpyxl") as writer:
            header_df.to_excel(writer, index=False, header=False, startrow=0)
            self.df.to_excel(writer, index=False, startrow=HEADER_ROW_INDEX)


async def main():
    print("=" * 100)
    print("EXCEL RAG PROCESSOR (Pandas)")
    print("Reads requirements, queries RAG agent, writes Recommendation/RAG Response/Source")
    print("=" * 100)
    print()

    data_dir = Path(__file__).parent.parent / "data"
    excel_file = data_dir / "Summary of Specific MCGCRT Record-2026-03-27-13-12-58.xlsx"

    if not excel_file.exists():
        print(f"Excel file not found: {excel_file}")
        return 1

    print(f"[OK] Excel file: {excel_file.name}")
    print()

    processor = ExcelRAGProcessor(str(excel_file))
    processor.load_excel()

    print(f"Requirements to process: {processor.df[REQUIREMENT_COL].notna().sum()}")
    print("This may take several minutes...")
    print()

    await processor.process_all(max_rows=None)

    processor.save_final()

    print("\n" + "=" * 100)
    print("PROCESSING COMPLETE")
    print("=" * 100)
    print(f"Processed: {processor.processed_count} | Errors: {processor.error_count}")
    print("Results written to columns: Recommendation, RAG Response, Source")
    print(f"Output file: {processor.excel_file_path.parent / 'rag_results.xlsx'}")
    print()

    return 0


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
