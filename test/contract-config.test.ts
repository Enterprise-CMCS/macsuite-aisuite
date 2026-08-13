import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import { resolveActiveContract } from "../src/contract-config";

const temporaryDirectories: string[] = [];

function writeContractConfig(contents: string): string {
  const directory = mkdtempSync(join(tmpdir(), "aisuite-contract-config-"));
  const iniPath = join(directory, "aws.properties.ini");
  temporaryDirectories.push(directory);
  writeFileSync(iniPath, contents);
  return iniPath;
}

afterEach(() => {
  for (const directory of temporaryDirectories.splice(0)) {
    rmSync(directory, { force: true, recursive: true });
  }
});

describe("resolveActiveContract", () => {
  it("resolves the active contract from the repository configuration", () => {
    expect(resolveActiveContract()).toEqual(
      expect.objectContaining({
        id: "tn_6756",
        inputPrefix: "state_of_TN/MCCRS-TN-6756-TennCare/",
        embeddingsTableName: "embeddings_tn_6756_tenncare",
        outputPrefix:
          "state_of_TN_bdaoutput/MCCRS-TN-6756-TennCare/bdaoutput/",
      }),
    );
  });

  it("rejects a configuration with no active contract", () => {
    const iniPath = writeContractConfig(`
[contract:first]
active = false
input_prefix = first/
output_prefix = first-output/
embeddings_table_name = embeddings_first
`);

    expect(() => resolveActiveContract(iniPath)).toThrowError(
      /No active contract section/i,
    );
  });

  it("rejects a configuration with multiple active contracts", () => {
    const iniPath = writeContractConfig(`
[contract:first]
active = true
input_prefix = first/
output_prefix = first-output/
embeddings_table_name = embeddings_first

[contract:second]
active = true
input_prefix = second/
output_prefix = second-output/
embeddings_table_name = embeddings_second
`);

    expect(() => resolveActiveContract(iniPath)).toThrowError(
      /Multiple active contract sections/i,
    );
  });
});
