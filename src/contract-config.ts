import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

export interface ActiveContract {
  embeddingsTableName: string;
  id: string;
  inputPrefix: string;
  outputPrefix: string;
}

type IniSection = Record<string, string>;

const DEFAULT_INI_PATH = fileURLToPath(
  new URL("../services/rag/common/utils/aws.properties.ini", import.meta.url),
);
const CONTRACT_SECTION_PREFIX = "contract:";
const TRUE_VALUES = new Set(["1", "yes", "true", "on"]);
const FALSE_VALUES = new Set(["0", "no", "false", "off"]);

function parseIni(contents: string): Map<string, IniSection> {
  const sections = new Map<string, IniSection>();
  let currentSection: IniSection | undefined;

  for (const rawLine of contents.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (line === "" || line.startsWith(";")) {
      continue;
    }

    const sectionMatch = /^\[([^\]]+)\]$/.exec(line);
    if (sectionMatch) {
      currentSection = {};
      sections.set(sectionMatch[1]!, currentSection);
      continue;
    }

    if (!currentSection) {
      continue;
    }

    const separatorIndex = line.indexOf("=");
    if (separatorIndex === -1) {
      continue;
    }

    const key = line.slice(0, separatorIndex).trim().toLowerCase();
    const value = line.slice(separatorIndex + 1).trim();
    currentSection[key] = value;
  }

  return sections;
}

function parseBoolean(value: string, sectionName: string): boolean {
  const normalizedValue = value.toLowerCase();
  if (TRUE_VALUES.has(normalizedValue)) {
    return true;
  }
  if (FALSE_VALUES.has(normalizedValue)) {
    return false;
  }

  throw new Error(
    `Invalid active value "${value}" in [${sectionName}]; expected true or false.`,
  );
}

function requiredValue(
  section: IniSection,
  sectionName: string,
  key: string,
): string {
  const value = section[key];
  if (value === undefined || value === "") {
    throw new Error(`Missing ${key} in active section [${sectionName}].`);
  }
  return value;
}

export function resolveActiveContract(
  iniPath: string = DEFAULT_INI_PATH,
): ActiveContract {
  const sections = parseIni(readFileSync(iniPath, "utf8"));
  const contractSections = [...sections.entries()].filter(([name]) =>
    name.startsWith(CONTRACT_SECTION_PREFIX),
  );
  const activeSections = contractSections.filter(([name, section]) => {
    const active = section.active;
    return active === undefined ? false : parseBoolean(active, name);
  });

  if (activeSections.length === 0) {
    throw new Error(
      "No active contract section: set active = true on exactly one [contract:…] section.",
    );
  }
  if (activeSections.length > 1) {
    const names = activeSections.map(([name]) => `[${name}]`).join(", ");
    throw new Error(
      `Multiple active contract sections (${names}); only one may have active = true.`,
    );
  }

  const [sectionName, section] = activeSections[0]!;
  return {
    embeddingsTableName: requiredValue(
      section,
      sectionName,
      "embeddings_table_name",
    ),
    id: sectionName.slice(CONTRACT_SECTION_PREFIX.length),
    inputPrefix: requiredValue(section, sectionName, "input_prefix"),
    outputPrefix: requiredValue(section, sectionName, "output_prefix"),
  };
}
