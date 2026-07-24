DESIGNATION_EXTRACTION_PROMPT = """
You are an expert in analyzing Government of India organizational structures and extracting designation hierarchies.

## Task:
Extract ALL designation entries from the provided input data. Each unique combination of (Designation + Wing/Division/Section) is ONE separate entry.

## Inputs:
- **Primary reference document Summaries:**
- **Ministry/State Name:**
- **Department/Organisation Name:**

## HIGHEST PRIORITY RULE — ROW-LEVEL EXTRACTION:

The input summary contains a table titled **"List of Designations along with their Wings / Divisions / Sections"** (typically in Part B, Section 1). This table is your PRIMARY extraction source.

**Every single row in that table = one entry in your output.** Do NOT collapse, merge, or deduplicate rows that share the same base designation title.

For example, if the table contains:
| 1 | Under Secretary | AVD-I |
| 2 | Under Secretary | AVD-II |
| 3 | Under Secretary | CS-II |

Your output MUST contain THREE separate entries — not one "Under Secretary" entry.

If no such table exists in the input, extract from the entire input data, treating each mention of a designation in a distinct organizational context as a separate entry.

## CRITICAL CONSTRAINTS:

### Strict Data Boundary Rules:
1. **ONLY use designations explicitly mentioned in the input data provided.**
2. **DO NOT invent, assume, or add any designations from:**
   - Your general knowledge of government hierarchies
   - Standard government structures not mentioned in the input
   - Other ministries/departments
   - External references or common administrative positions
3. **If a designation is not in the input data, DO NOT include it — even if it seems logical or standard.**
4. If the provided input data contains absolutely NO designations, return an empty list for the designations array.

## Extraction Rules:

### 1. Row-Level Fidelity (HIGHEST PRIORITY):
   - Copy each row from the designations table exactly as it appears.
   - Preserve the EXACT designation name, spelling, abbreviations, and suffixes from the source.
   - Preserve the EXACT wing/division/section text from the source.
   - If the table has 80 rows, your output MUST have 80 entries (unless there are true duplicates — same designation AND same wing/division/section).
   - **Count check:** Before finalizing, count the rows in the source table and count your output entries. They should match (minus any exact duplicates).

### 2. Anti-Merging Safeguard:
   - **NEVER merge rows that share a base designation title but have different wings/divisions/sections.**
   - "Under Secretary | AVD-I" and "Under Secretary | AVD-II" are TWO separate entries.
   - "Director | AIS" and "Director | CS-I" are TWO separate entries.
   - "Joint Secretary | N/A" appearing once = one entry. Do not split it; do not merge it with other Joint Secretary entries.
   - When in doubt about whether two entries are the same, list them SEPARATELY.

### 3. Hierarchical Classification:
   - Assign `sort_order` as a strictly increasing integer starting from 1, with no gaps.
   - **Primary sort: by seniority tier** (highest rank first):
     - Tier 1: Secretary / Principal Secretary / Establishment Officer
     - Tier 2: Additional Secretary
     - Tier 3: Joint Secretary
     - Tier 4: Director
     - Tier 5: Deputy Secretary
     - Tier 6: Under Secretary
     - Tier 7: Section Officer / Assistant Section Officer
     - Tier 8: Support Staff (Secretariat Assistants, Stenographers, PA, PS, MTS, etc.)
   - **Secondary sort within the same tier: alphabetical by wing/division/section name.**
   - Each entry gets its own unique `sort_order` number, even if they share the same rank tier.

### 4. Wing/Division/Section Field:
   - Copy the wing/division/section EXACTLY as written in the source table — character-for-character, including slashes, ampersands, parentheses, and spacing.
   - If the source table shows "N/A" or the column is empty, use "N/A".
   - Do NOT invent, paraphrase, expand, or substitute any wing/division/section name with similar-sounding text from your general knowledge.
   - ⚠️ Example: if the table says "Planning & Training" → output must be "Planning & Training". Never "Decentralized Planning & Convergence" or "Capacity Building & Training".

## Example:

Suppose the input summary contains this table:
| S.No | Designation (Full Name) | Wing / Division / Section |
| 1 | Additional Secretary | N/A |
| 2 | Director | AIS |
| 3 | Director | CS-I |
| 4 | Under Secretary | AVD-I |
| 5 | Under Secretary | AVD-II |
| 6 | Under Secretary | Training |

## Expected Output:
Here is how you should generate an answer based on the received input data:
```json
{{
   "designations": [
      {{
         "sort_order": 1,
         "designation": "Additional Secretary",
         "wing_division_section": "N/A"
      }},
      {{
         "sort_order": 2,
         "designation": "Director",
         "wing_division_section": "AIS"
      }},
      {{
         "sort_order": 3,
         "designation": "Director",
         "wing_division_section": "CS-I"
      }},
      {{
         "sort_order": 4,
         "designation": "Under Secretary",
         "wing_division_section": "AVD-I"
      }},
      {{
         "sort_order": 5,
         "designation": "Under Secretary",
         "wing_division_section": "AVD-II"
      }},
      {{
         "sort_order": 6,
         "designation": "Under Secretary",
         "wing_division_section": "Training"
      }}
   ]
}}
```
Note: 6 table rows → 6 entries (no merging)

## OUTPUT FORMAT:
Provide the output strictly as a valid JSON object matching the schema below. Do not include introductory text, explanations, or markdown outside of the JSON block.

{{
   "designations": [
      {{
         "sort_order": integer,
         "designation": "string",
         "wing_division_section": "string"
      }}
   ]
}}
"""

# Use this prompt when not working with a multi-summary input
DESIGNATION_EXTRACTION_PROMPT_V2 = """
You are an expert in analyzing Government of India organizational structures and extracting designation hierarchies.

## Task:
Extract ALL designation entries from the provided input data. The input may contain ONE or MULTIPLE document summaries. Each unique combination of (Designation + Wing/Division/Section) is ONE separate entry.

## Inputs:
- **Primary reference document Summaries:** (one or more summaries)
- **Ministry/State Name:**
- **Department/Organisation Name:**

## ============================================================
## HIGHEST PRIORITY RULE — ROW-LEVEL EXTRACTION FROM ALL TABLES
## ============================================================

### Step 1: Locate ALL Designation Tables
Scan ALL provided summaries for tables titled **"List of Designations along with their Wings / Divisions / Sections"** (typically in Part B, Section 1 of each summary).

- If there is ONE summary → extract from that one table.
- If there are MULTIPLE summaries → extract from ALL tables across ALL summaries.

### Step 2: UNION All Table Rows
Combine (UNION) all rows from all designation tables into a single master list.

**Every single row from every table = one candidate entry in your output.**

Do NOT collapse, merge, or deduplicate rows that share the same base designation title but have DIFFERENT wings/divisions/sections.

For example, if Summary 1 contains:
| 1 | Under Secretary | AVD-I |
| 2 | Under Secretary | AVD-II |

And Summary 2 contains:
| 1 | Under Secretary | AVD-II |
| 2 | Under Secretary | CS-II |
| 3 | Director | Training |

Your combined master list has 5 candidate rows. After deduplication (Step 3), "Under Secretary | AVD-II" appears in both, so the final output has 4 entries.

### Step 3: Cross-Summary Deduplication
After combining all rows, remove TRUE duplicates only. Two rows are TRUE duplicates if and only if:
- The designation name is IDENTICAL (or clearly the same abbreviation — see normalization rules below), AND
- The wing/division/section is IDENTICAL (or clearly the same unit — see normalization rules below)

**Normalization Rules for Deduplication:**
- Treat these as the SAME: "AVD-I" = "AVD - I" = "AVD-1" (whitespace/hyphen/numeral variants of the same unit)
- Treat these as the SAME: "Pers. Policy" = "Pers.Policy" = "PERS POLICY" (spacing and case variants)
- Treat these as DIFFERENT: "AVD-I" ≠ "AVD-II" (different unit numbers)
- Treat these as DIFFERENT: "AIS" ≠ "AIS Division" UNLESS the context clearly shows they refer to the same organizational unit
- Treat these as DIFFERENT: "Director | AIS" ≠ "Director | N/A" (one has a specific wing, the other doesn't)
- **When in doubt, keep BOTH entries as separate.** It is better to have a near-duplicate than to accidentally lose a distinct entry.

**Which version to keep for true duplicates:**
- Prefer the version with the MORE SPECIFIC wing/division/section name.
- If specificity is equal, prefer the version from the FIRST summary (i.e., the summary that appears earliest in the input).

### Step 4: Scan Narratives for Additional Designations
After processing all tables, scan the ENTIRE input (all summaries — Part A sections, competency sections, mapping tables, training sections) for any designations mentioned in narrative text that are NOT already in your master list.

Add these as additional entries. For narrative-sourced designations:
- Derive the wing/division/section from the surrounding context.
- If no specific unit is mentioned, use the most relevant context (e.g., "N/A", "Training Division", "Field Level", "Cadre Management").

### Step 5: Assign Sort Order
Assign `sort_order` as a strictly increasing integer starting from 1.

**Primary sort: by seniority tier** (highest rank first):
- Tier 1: Secretary / Establishment Officer & Additional Secretary / Principal Secretary
- Tier 2: Additional Secretary
- Tier 3: Joint Secretary
- Tier 4: Director
- Tier 5: Deputy Secretary
- Tier 6: Under Secretary
- Tier 7: Section Officer / Assistant Section Officer
- Tier 8: Support Staff (Secretariat Assistants, Stenographers, PA, PS, PPS, PSO, MTS, etc.)

**Secondary sort within the same tier: alphabetical by wing/division/section name.**
- "N/A" entries sort FIRST within their tier (before alphabetical entries).
- Each entry gets its own unique `sort_order` number.


## CRITICAL CONSTRAINTS:

### Strict Data Boundary Rules:
1. **ONLY use designations explicitly mentioned in the input data provided.**
2. **DO NOT invent, assume, or add any designations from:**
   - Your general knowledge of government hierarchies
   - Standard government structures not mentioned in the input
   - Other ministries/departments
   - External references or common administrative positions
3. **If a designation is not in the input data, DO NOT include it — even if it seems logical or standard.**
4. If the provided input data contains absolutely NO designations, return an empty list.

### Anti-Merging Safeguard:
- **NEVER merge rows that share a base designation title but have different wings/divisions/sections.**
- "Under Secretary | AVD-I" and "Under Secretary | AVD-II" are TWO separate entries.
- "Director | AIS" and "Director | CS-I" are TWO separate entries.
- When in doubt about whether two entries are the same, list them SEPARATELY.

### Wing/Division/Section Field:
- Copy the wing/division/section EXACTLY as written in the source table — character-for-character, including slashes, ampersands, parentheses, and spacing.
- If the source table shows "N/A" or the column is empty, use "N/A".
- Do NOT invent, paraphrase, expand, or substitute any wing/division/section name with similar-sounding text from your general knowledge.
- ⚠️ Example: if the table says "Planning & Training" → output must be "Planning & Training". Never "Decentralized Planning & Convergence" or "Capacity Building & Training".

### Compound Designation Splitting:
- If any table row lists two or more job titles joined by " / " or " and " (e.g., "Deputy Directors / Assistant Directors", "Accounts Officer / Finance Officer"), treat each as a **separate entry** with the same wing/division/section value.
- For example, a single row "Deputy Directors / Assistant Directors | N/A" must become TWO entries:
  - "Deputy Directors" with wing_division_section "N/A"
  - "Assistant Directors" with wing_division_section "N/A"
- Apply this splitting rule before deduplication and before assigning sort_order.

## Example with Multiple Summaries:

Suppose the input contains two summaries:

**Summary 1 table:**
| S.No | Designation (Full Name) | Wing / Division / Section |
| 1 | Additional Secretary | N/A |
| 2 | Joint Secretary | N/A |
| 3 | Director | AIS |
| 4 | Under Secretary | AVD-I |
| 5 | Under Secretary | CS-II |

Summary 1 narrative also mentions: "Section Officer" in Part A.

**Summary 2 table:**
| S.No | Designation (Full Name) | Wing / Division / Section |
| 1 | Joint Secretary | N/A |
| 2 | Director | AIS |
| 3 | Director | Training |
| 4 | Under Secretary | AVD-I |
| 5 | Under Secretary | AVD-II |
| 6 | Under Secretary | Training |

Summary 2 narrative also mentions: "Secretariat Assistant" and "MTS" in Part A.

**Processing:**
- Summary 1 table: 5 rows
- Summary 2 table: 6 rows
- Total candidate rows: 11
- True duplicates across summaries: "Joint Secretary | N/A", "Director | AIS", "Under Secretary | AVD-I" → remove 3
- Unique table entries: 8
- Narrative-only additions: "Section Officer", "Secretariat Assistant", "MTS" → add 3
- Final count: 11

## Expected Output:
{{
   "designations": [
      {{
         "sort_order": 1,
         "designation": "Additional Secretary",
         "wing_division_section": "N/A"
      }},
      {{
         "sort_order": 2,
         "designation": "Joint Secretary",
         "wing_division_section": "N/A"
      }},
      {{
         "sort_order": 3,
         "designation": "Director",
         "wing_division_section": "AIS"
      }},
      {{
         "sort_order": 4,
         "designation": "Director",
         "wing_division_section": "Training"
      }},
      {{
         "sort_order": 5,
         "designation": "Under Secretary",
         "wing_division_section": "AVD-I"
      }},
      {{
         "sort_order": 6,
         "designation": "Under Secretary",
         "wing_division_section": "AVD-II"
      }},
      {{
         "sort_order": 7,
         "designation": "Under Secretary",
         "wing_division_section": "CS-II"
      }},
      {{
         "sort_order": 8,
         "designation": "Under Secretary",
         "wing_division_section": "Training"
      }},
      {{
         "sort_order": 9,
         "designation": "Section Officer",
         "wing_division_section": "N/A"
      }},
      {{
         "sort_order": 10,
         "designation": "Secretariat Assistant",
         "wing_division_section": "N/A"
      }},
      {{
         "sort_order": 11,
         "designation": "MTS",
         "wing_division_section": "N/A"
      }}
   ]
}}

Note: 5 + 6 = 11 total rows, minus 3 true duplicates = 8, plus 3 narrative-only = 11 final entries.


## OUTPUT FORMAT:
Provide the output strictly as a valid JSON object matching the schema below. Do not include introductory text, explanations, or markdown outside of the JSON block.

{{
   "designations": [
      {{
         "sort_order": integer,
         "designation": "string",
         "wing_division_section": "string"
      }}
   ]
}}
"""

ROLE_MAPPING_PROMPT_CENTRE ="""
You are an expert in Mission Karmayogi and competency role mapping specialist (FRAC - Framework of Roles, Activities, and Competencies mapping) for Government of India officials.

## Task:
Generate comprehensive, structured, and hierarchically sorted FRAC mappings for all designations from the validated designations list, detailing the roles & responsibilities, activities, and competencies of Government of India officials based on the provided input data, and deliver the output in JSON format.

## Inputs:
You will be provided with the following inputs:
- **Validated Designations List:** Pre-extracted list of designations, along with their Wing/Division/Section, that have already been validated in Pass 1. Each entry contains `designation`, `wing_division_section`, and `sort_order`. This is your definitive list of designations for which you must generate FRAC mappings.
- **Primary reference document summaries:** Work Allocation Order / Annual Capacity Building Plan (ACBP) / schemes / missions / programs / policies summaries. These provide a comprehensive understanding of the ministry’s strategic objectives, capacity-building requirements, and the broader context that shapes its schemes, programmes, and priority areas. They also outline the complete designation hierarchy, specific roles, responsibilities, and work allocations, supported by a detailed depiction of the organisational structure.
- **KCM (Karmayogi Competency Model) Dataset:** A flat list of all valid Behavioral and Functional competencies with descriptions. Use it exclusively for selecting and copying Behavioral and Functional competencies — detailed usage instructions are in Section 2 and in the dataset block below.
- **Ministry/Organization Name:** The name of the ministry/organisation being analyzed.
- **Department Name:** The specific department/organisation, if applicable.
- **Additional Instructions:** Any other specific guidelines to get more relevant outcomes/results.

## Rules:

### Section 1: Designation Handling & Role Architecture Rules

1.1. **Validated Designations — Use As-Is:**
    - The **Validated Designations List** provided from Pass 1 is your FINAL designation list. Each entry includes the `designation` name, its `wing_division_section` (organizational unit), and `sort_order` (hierarchy position).
    - **DO NOT** re-extract, add, remove, rename, merge, or modify any designation from this list.
    - You MUST generate a FRAC mapping entry for **EVERY designation** in the Validated Designations List — no exceptions, no skipping.
    - If the Validated Designations List contains 50 designations, your output JSON MUST contain exactly 50 objects.
    - **Use the `wing_division_section` context** from each entry.
    - **Use the `sort_order`** to maintain the same hierarchical ordering in your output.
    - If a designation has limited information in the document summaries, still generate a mapping using the `wing_division_section` context, the designation title, and reasonable inference from its seniority level and organisational placement.

1.2. **Roles Must Reflect Full Scope:**
    - Do NOT restrict roles to administrative and compliance functions only.
    - For every designation, explicitly include roles covering:
        - Leadership and decision-making (where applicable to seniority).
        - Facilitation and coordination (internal and external).
        - Stakeholder engagement and communication.
        - Developmental and capacity-building responsibilities.
        - Monitoring, evaluation, and accountability.
    - Roles should be derived primarily from the uploaded source documents. Do not default to generic role labels when document-specific roles are available.

1.3. **Activity Quality Standards:**
    - Each activity MUST be:
        - **Specific** — describes a concrete task, not a vague function.
        - **Action-oriented** — starts with an active verb (e.g., "Draft", "Review", "Coordinate", "Monitor", "Analyse").
        - **Non-repetitive** — the same task must not appear under multiple roles or activities.
        - **Aligned** — clearly traceable to the role under which it is listed.
    - Activities must NOT simply restate the role label as a sentence.

1.4. **Mandatory Centre–State Coordination:**
    - For **ALL** senior-level designations (Secretary, Additional Secretary, Joint Secretary, Director), you **MUST** explicitly include "Coordination with State Governments for scheme implementation, policy feedback, and capacity building" as a key role and responsibility.

### Section 2: Competency Mapping Rules

2.1. **Minimum Coverage Requirements**
- **Behavioral:** MINIMUM 4, MAXIMUM 6 competencies per designation.
- **Functional:** MINIMUM 3, MAXIMUM 6 competencies per designation.
- **Domain:** MINIMUM 8, MAXIMUM 10 competencies per designation.
- Do not exceed these ceilings. Prioritise the most critical competencies for the designation’s actual role rather than mapping exhaustively.

2.2. **Behavioral & Functional Competencies — SELECT BY ID**
- ⚠️ **ABSOLUTE RULE — KCM-ONLY:** Every Behavioral and Functional competency you output MUST be one you SELECTED from the provided KCM Dataset by its `competency_id`. This is a hard constraint with zero exceptions.
- **How to select:** read the `type`, `theme`, `theme_description`, `sub_theme`, and `sub_theme_description` of the KCM entries and decide which genuinely fit the designation's role and activities. Selection is a judgement over the descriptions — read them carefully.
- **How to output:** for each competency you select, output its `competency_id` exactly as written in the dataset (e.g. `BEH-007`, `FUN-045`), and copy the `type`, `theme`, and `sub_theme` from the SAME entry as that id. The id and the three fields must all come from ONE entry — never combine a theme from one entry with a sub_theme from another.
- Do NOT invent, paraphrase, rename, shorten, or lengthen any Behavioral or Functional competency, and do NOT emit a `competency_id` that is not in the dataset. Anything not selectable by a real KCM id will be discarded downstream.
- Do NOT use your general knowledge to generate Behavioral or Functional competencies — the KCM Dataset is the only permitted source.
- (Domain competencies have NO `competency_id` — leave that field out for Domain.)

**Relevance guardrails — pick for THIS role, not just any valid entry:**
- **Ground every pick.** Select a Behavioral/Functional competency ONLY if you can tie it to a specific listed Role/Responsibility or Activity of THIS designation. Silently ask yourself "which duty of this role needs this competency?" — if you cannot answer, do NOT select it. A valid KCM entry that is irrelevant to the role is still a WRONG selection.
- **Do not pad to a count.** Choose the most relevant entries first. Aim for at least the minimum, but NEVER add a clearly-irrelevant competency just to reach the minimum or approach the maximum. Fewer, genuinely-relevant competencies are better than padded ones.
- **Administrative/office guardrail.** Administrative or office-function competencies (e.g. Office Management, Establishment & HR, Handling Leave & Travel, Financial/Expenditure Management, Procurement, File/Records management) may be assigned ONLY to designations whose actual duties include those administrative functions (e.g. clerical, HR, accounts, secretariat roles). Do NOT assign them to purely field, operational, security, or technical roles (e.g. Constable, Driver, Sweeper, Cleaner, Technician, Guard) unless that role genuinely performs office/admin work. For such field/operational roles, prefer functional competencies that match their actual on-ground work.

2.3. **Domain Competencies**
- **Mandatory Scheme & Policy Coverage:** All significant missions, schemes, flagship programs, acts, and policies mentioned in the source documents MUST be reflected as specific domain competencies for the relevant designations. No major initiative should be left unmapped.
- **Expanded Scope:** Domain competencies MUST cover:
    - Departmental/organisational Schemes & Missions.
    - Financial & Administrative Management (e.g., GFR, PFMS).
    - State Coordination Mechanisms.
    - The Legislative & Regulatory Framework (relevant Acts, Rules, and policies).
- **Secretary-Level Mandate:** For the highest-ranking official (e.g., Secretary), include domain competencies covering ‘Policy review/validation’, ‘Scheme Architecture review/validation’, and ‘Governance & Institutional Design’.
- **Standardised Taxonomy:** All domain competency labels must follow a consistent naming structure: `[Theme] — [Sub-theme/Specific Area]` (e.g., "Scheme Management — PMAY Urban Implementation"). Do not use vague labels like "Policy" or "Governance" alone.
- **AI-Enriched Generation:** Augment domain competencies with synthesised knowledge including:
    - Relevant international best practices and conventions (e.g., UN, World Bank reports, CEDAW, UNCRC).
    - Comparable state-level schemes and policies for holistic federal context.

### Section 3: Output Format & Structure Rules

3.1. **Format**: The final output MUST be a single, valid JSON array of objects.

3.2. **Hierarchical Sorting**: The JSON array MUST be sorted in descending order of hierarchy, starting from the highest designation (e.g., Secretary) and proceeding down to junior-most staff.
`sort_order` must be a strictly increasing integer starting from 1 (e.g., 1, 2, 3, 4, 5...), without skipping or jumping numbers. The sequence must follow numeric order, not string/lexical order.

3.3. **JSON Schema**: Each entry MUST follow this exact structure:
{output_json_format}

⚠️ CRITICAL OUTPUT FORMAT RULES — VIOLATIONS WILL CAUSE SYSTEM FAILURE:
1. `competencies` MUST be a **flat JSON array** of objects. Do NOT group by type. The following structure is STRICTLY FORBIDDEN:
   {{"behavioral": [...], "functional": [...], "domain": [...]}}
   The ONLY valid structure is: [{{"competency_id": "BEH-007", "type": "Behavioral", "theme": "...", "sub_theme": "..."}}, ...]  (competency_id required for Behavioral/Functional; omit it for Domain)
2. `role_responsibilities` MUST be a **flat array of strings** at the top level of each object. Do NOT nest it inside roles or any other key.
3. `activities` MUST be a **flat array of strings** at the top level of each object. Do NOT nest it inside roles or any other key.
4. These rules apply to EVERY object in the output array — including lower-rank and support staff designations.

[START OF INPUT DATA]

### Validated Designations List (from PASS 1):
{pass1_output}

### Primary reference document summaries:
{primary_summary}

### KCM (Karmayogi Competency Model) Dataset:
The dataset below is a flat list of ALL valid Behavioral and Functional competencies. Each entry is one complete, selectable competency.

Field usage:
- `competency_id`: The stable KCM id (e.g. `BEH-007`, `FUN-045`) — **this is what you select and MUST output** for every Behavioral/Functional competency. Output it exactly as written.
- `type`: "Behavioral" or "Functional" — identifies the competency category. Copy from the selected entry.
- `theme`: The competency theme name — copy from the SAME entry as the `competency_id`.
- `theme_description`: What the theme means — use this to judge whether the theme is relevant to the designation's overall role. Do NOT output this field.
- `sub_theme`: The competency sub-theme name — copy from the SAME entry as the `competency_id`.
- `sub_theme_description`: What the sub-theme means — use this to judge whether it fits the designation's specific activities. Do NOT output this field.

Selection process: For each designation, read the `theme_description` and `sub_theme_description` of candidate entries to assess fit against the designation's actual roles and activities. Only select entries where the description genuinely matches the role context. Output the chosen entry's `competency_id` and copy its `type`, `theme`, and `sub_theme` verbatim from that one entry — no paraphrasing, no renaming, no mixing fields across entries.

{kcm_competencies}

### Ministry/Organization Name:
{organization_name}

### Department Name:
{department_name}

### Additional Instructions:
{instructions}

[END OF INPUT DATA]
"""

# Per-designation DOMAIN-from-WAO prompt (PASS 3 in role_mapping_service). Reads the verbatim
# WAO for ONE designation and returns every domain competency implied by its allocated work
# (uncapped, bounded output per call). Behavioural/Functional are unchanged (still KCM-based).
DOMAIN_FROM_WAO_PROMPT = """You are mapping DOMAIN competencies for a specific Government of India role, \
using its Work Allocation Order (WAO) as the primary and authoritative source.

The department's Work Allocation Order (WAO) is provided as the attached PDF document(s). Treat it as \
the authoritative source and read it directly — including any scanned pages, tables and column layouts.

ORGANISATION: {organization_name}
DEPARTMENT: {department_name}
DESIGNATION: {designation}
WING / DIVISION / SECTION: {wing}

TASK:
- Read the WAO and locate THIS designation's allocated roles, responsibilities and activities.
- Map EVERY distinct domain competency implied by that allocated work: each function, scheme, \
programme, mission, statute, regulation, rule, duty and power becomes a SEPARATE domain competency.
- Be EXHAUSTIVE. Do NOT cap, trim, summarise, or limit to a fixed number - a role with broad \
allocations should produce many competencies. Only include competencies genuinely grounded in the \
WAO for this role; do not invent unrelated ones.
- Use the standardised taxonomy label: "[Theme] - [Sub-theme/Specific Area]".

Output ONLY a JSON array of objects, each: {{"theme": "...", "sub_theme": "..."}}."""


ROLE_MAPPING_PROMPT_STATE ="""
You are an expert in Mission Karmayogi and competency role mapping (FRAC - Framework of Roles, Activities, and Competencies mapping) for Government of India officials.

## Task:
Generate comprehensive, structured, and hierarchically sorted FRAC mappings for all designations from the validated designations list, detailing the roles & responsibilities, activities, and competencies of State Government officials based on the provided input data, and deliver the output in JSON format.

## Inputs:
You will be provided with the following inputs:
- **Validated Designations List:** Pre-extracted list of designations, along with their Wing/Division/Section, that have already been validated in Pass 1. Each entry contains `designation`, `wing_division_section`, and `sort_order`. This is your definitive list of designations for which you must generate FRAC mappings.
- **Primary reference document summaries:** The primary documents summarise the State Department’s key priorities, capacity-building needs, and the context behind its schemes, missions, and policies. They outline the department’s hierarchy, major designations, and their specific work allocations, along with a clear view of the organisational structure across state, district, and field levels, providing understanding of roles, responsibilities, and how departmental functions are executed within the State’s administrative framework.
- **KCM (Karmayogi Competency Model) Dataset:** A flat list of all valid Behavioral and Functional competencies with descriptions. Use it exclusively for selecting and copying Behavioral and Functional competencies — detailed usage instructions are in Section 2 and in the dataset block below.
- **State Name:** The name of the state being analyzed, for geographical and development context specific to the department/organisation.
- **Department/Organisation Name:** The specific department/organisation, if applicable.
- **Additional Instructions:** Any other specific guidelines to improve Domain competency generation.

## Rules:

### Section 1: Designation Handling & Role Architecture Rules

1.1. **Validated Designations — Use As-Is:**
    - The **Validated Designations List** provided from Pass 1 is your FINAL designation list. Each entry includes the `designation` name, its `wing_division_section` (organizational unit), and `sort_order` (hierarchy position).
    - **DO NOT** re-extract, add, remove, rename, merge, or modify any designation from this list.
    - You MUST generate a FRAC mapping entry for **EVERY designation** in the Validated Designations List — no exceptions, no skipping.
    - If the Validated Designations List contains 50 designations, your output JSON MUST contain exactly 50 objects.
    - **Use the `wing_division_section` context** from each entry.
    - **Use the `sort_order`** to maintain the same hierarchical ordering in your output.
    - If a designation has limited information in the document summaries, still generate a mapping using the `wing_division_section` context, the designation title, and reasonable inference from its seniority level and organisational placement.

1.2. **Roles Must Reflect Full Scope:**
    - Do NOT restrict roles to administrative and compliance functions only.
    - For every designation, explicitly include roles covering:
        - Leadership and decision-making (where applicable to seniority).
        - Facilitation and coordination (internal, inter-departmental, and Centre–State).
        - Stakeholder engagement and community/beneficiary interface.
        - Developmental, capacity-building, and field-level implementation responsibilities.
        - Monitoring, evaluation, and accountability.
    - Roles should be derived primarily from the uploaded source documents. Do not default to generic role labels when document-specific roles are available.

1.3. **Activity Quality Standards:**
    - Each activity MUST be:
        - **Specific** — describes a concrete task, not a vague function.
        - **Action-oriented** — starts with an active verb (e.g., “Draft”, “Review”, “Coordinate”, “Monitor”, “Analyse”, “Verify”, “Compile”).
        - **Non-repetitive** — the same task must not appear under multiple roles or activities.
        - **Aligned** — clearly traceable to the role under which it is listed.
    - Activities must NOT simply restate the role label as a sentence.

1.4. **Mandatory State Coordination/Implementation:**
    - For **ALL** senior-level designations (Secretary, Principal Secretary, Additional Secretary, Joint Secretary, Director), you **MUST** explicitly include “Coordination within the State Government and with Central Government for scheme implementation, policy feedback, and capacity building” as a key role and responsibility.

### Section 2: Competency Mapping Rules

2.1. **Minimum Coverage Requirements**
- **Behavioral:** MINIMUM 4, MAXIMUM 6 competencies per designation.
- **Functional:** MINIMUM 3, MAXIMUM 6 competencies per designation.
- **Domain:** MINIMUM 8, MAXIMUM 10 competencies per designation.
- Do not exceed these ceilings. Prioritise the most critical competencies for the designation’s actual role rather than mapping exhaustively.

2.2. **Behavioral & Functional Competencies — SELECT BY ID**
- ⚠️ **ABSOLUTE RULE — KCM-ONLY:** Every Behavioral and Functional competency you output MUST be one you SELECTED from the provided KCM Dataset by its `competency_id`. This is a hard constraint with zero exceptions.
- **How to select:** read the `type`, `theme`, `theme_description`, `sub_theme`, and `sub_theme_description` of the KCM entries and decide which genuinely fit the designation's role and activities. Selection is a judgement over the descriptions — read them carefully.
- **How to output:** for each competency you select, output its `competency_id` exactly as written in the dataset (e.g. `BEH-007`, `FUN-045`), and copy the `type`, `theme`, and `sub_theme` from the SAME entry as that id. The id and the three fields must all come from ONE entry — never combine a theme from one entry with a sub_theme from another.
- Do NOT invent, paraphrase, rename, shorten, or lengthen any Behavioral or Functional competency, and do NOT emit a `competency_id` that is not in the dataset. Anything not selectable by a real KCM id will be discarded downstream.
- Do NOT use your general knowledge to generate Behavioral or Functional competencies — the KCM Dataset is the only permitted source.
- (Domain competencies have NO `competency_id` — leave that field out for Domain.)

**Relevance guardrails — pick for THIS role, not just any valid entry:**
- **Ground every pick.** Select a Behavioral/Functional competency ONLY if you can tie it to a specific listed Role/Responsibility or Activity of THIS designation. Silently ask yourself "which duty of this role needs this competency?" — if you cannot answer, do NOT select it. A valid KCM entry that is irrelevant to the role is still a WRONG selection.
- **Do not pad to a count.** Choose the most relevant entries first. Aim for at least the minimum, but NEVER add a clearly-irrelevant competency just to reach the minimum or approach the maximum. Fewer, genuinely-relevant competencies are better than padded ones.
- **Administrative/office guardrail.** Administrative or office-function competencies (e.g. Office Management, Establishment & HR, Handling Leave & Travel, Financial/Expenditure Management, Procurement, File/Records management) may be assigned ONLY to designations whose actual duties include those administrative functions (e.g. clerical, HR, accounts, secretariat roles). Do NOT assign them to purely field, operational, security, or technical roles (e.g. Constable, Driver, Sweeper, Cleaner, Technician, Guard) unless that role genuinely performs office/admin work. For such field/operational roles, prefer functional competencies that match their actual on-ground work.

2.3. **Domain Competencies**
- **Exhaustive Scheme & Policy Coverage:** Every significant mission, scheme, flagship programme, statute, regulation, or policy explicitly or implicitly referenced in the source documents must be captured as a specific domain competency tied to the relevant designation(s). No major initiative should be left unmapped.
    - Map each initiative as a distinct competency using standardised taxonomy: `[Theme] — [Sub-theme/Specific Area]` (e.g., “Scheme Management — PMAY-U Affordable Housing Implementation”, “Legislative Compliance — State Land Acquisition Act”).
    - If a document mentions multiple tiers/variants of a scheme (state-specific adaptation, central+state co-funded model), map each variant separately.
- **Expanded Scope:** Domain competencies MUST cover:
    - Departmental schemes, missions & programmes — operational, implementation, and evaluation competencies.
    - Financial & administrative management — e.g., GFR compliance, PFMS operations, state treasury workflows, budget execution & reporting.
    - Inter- and intra-state coordination mechanisms — Nodal agency coordination, interstate committees, inter-departmental taskforces, Centre–State liaison, and disaster response coordination. (Mandatory)
    - Legislative & regulatory framework — relevant State Acts, statutory rules, notifications, and compliance obligations (include both central laws as applicable in the state and state-specific statutes).
- **Secretary-Level Mandate:** For the highest state officials (Secretary / Principal Secretary / Head of Department), include strategic, high-impact competencies covering:
    - Policy review, implementation & strategy design (framing policy objectives, stakeholder consultations, evidence synthesis).
    - Scheme architecture & program design (funding model, delivery architecture, outcome metrics, M&E design).
    - Governance & institutional design (organisational mandates, delegation of powers, accountability mechanisms).
    - State-level fiscal stewardship (state budget strategy, fiscal risk management, inter-governmental transfers).
- **AI-Enriched Augmentation:**
    - Augment explicit source content with synthesised, high-quality contextual knowledge:
        - Relevant international best practices and conventions (e.g., UN guidance, World Bank implementation notes, CEDAW/UNCRC where gender/child rights are relevant). Mark these as contextual references — they do not replace source-specific requirements.
        - Comparable state-level schemes and adaptations for federal context. Where a central scheme has multiple state delivery models, reference examples as optional competency variants.
        - When adding external context, indicate the source-type (e.g., “International best practice — World Bank guidance on beneficiary targeting”) and do not present it as if present in the uploaded documents.
- All domain competency labels MUST follow the standardised taxonomy structure: `[Theme] — [Sub-theme/Specific Area]` to ensure uniformity and traceability.

### Section 3: Output Format & Structure Rules

3.1. **Format**: The final output MUST be a single, valid JSON array of objects.

3.2. **Hierarchical Sorting**: The JSON array MUST be sorted in descending order of hierarchy, starting from the highest designation (e.g., Secretary) and proceeding down to junior-most staff.
`sort_order` must be a strictly increasing integer starting from 1 (e.g., 1, 2, 3, 4, 5...), without skipping or jumping numbers. The sequence must follow numeric order, not string/lexical order.

3.3. **JSON Schema**: Each entry MUST follow this exact structure:
{output_json_format}

⚠️ CRITICAL OUTPUT FORMAT RULES — VIOLATIONS WILL CAUSE SYSTEM FAILURE:
1. `competencies` MUST be a **flat JSON array** of objects. Do NOT group by type. The following structure is STRICTLY FORBIDDEN:
   {{"behavioral": [...], "functional": [...], "domain": [...]}}
   The ONLY valid structure is: [{{"competency_id": "BEH-007", "type": "Behavioral", "theme": "...", "sub_theme": "..."}}, ...]  (competency_id required for Behavioral/Functional; omit it for Domain)
2. `role_responsibilities` MUST be a **flat array of strings** at the top level of each object. Do NOT nest it inside roles or any other key.
3. `activities` MUST be a **flat array of strings** at the top level of each object. Do NOT nest it inside roles or any other key.
4. These rules apply to EVERY object in the output array — including lower-rank and support staff designations.

[START OF INPUT DATA]

### Validated Designations List (from PASS 1):
{pass1_output}

### Primary reference document summaries:
{primary_summary}

### KCM (Karmayogi Competency Model) Dataset:
The dataset below is a flat list of ALL valid Behavioral and Functional competencies. Each entry is one complete, selectable competency.

Field usage:
- `competency_id`: The stable KCM id (e.g. `BEH-007`, `FUN-045`) — **this is what you select and MUST output** for every Behavioral/Functional competency. Output it exactly as written.
- `type`: "Behavioral" or "Functional" — identifies the competency category. Copy from the selected entry.
- `theme`: The competency theme name — copy from the SAME entry as the `competency_id`.
- `theme_description`: What the theme means — use this to judge whether the theme is relevant to the designation's overall role. Do NOT output this field.
- `sub_theme`: The competency sub-theme name — copy from the SAME entry as the `competency_id`.
- `sub_theme_description`: What the sub-theme means — use this to judge whether it fits the designation's specific activities. Do NOT output this field.

Selection process: For each designation, read the `theme_description` and `sub_theme_description` of candidate entries to assess fit against the designation's actual roles and activities. Only select entries where the description genuinely matches the role context. Output the chosen entry's `competency_id` and copy its `type`, `theme`, and `sub_theme` verbatim from that one entry — no paraphrasing, no renaming, no mixing fields across entries.

{kcm_competencies}

### State Name:
{organization_name}

### Department/Organisation Name:
{department_name}

### Additional Instructions:
{instructions}

[END OF INPUT DATA]
"""
