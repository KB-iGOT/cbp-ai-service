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
   - Assign `sort_order` as a strictly increasing integer starting from 1.
   - **Primary sort: by seniority tier** (highest rank first):
     - Tier 1: Secretary / Establishment Officer & Additional Secretary
     - Tier 2: Additional Secretary
     - Tier 3: Joint Secretary
     - Tier 4: Director
     - Tier 5: Deputy Secretary
     - Tier 6: Under Secretary
     - Tier 7: Section Officer / Assistant Section Officer
     - Tier 8: Support Staff (Secretariat Assistants, Stenographers, PA, PS, MTS, etc.)
   - **Secondary sort within the same tier: alphabetical by wing/division/section name.**
   - Each entry gets its own unique `sort_order` number, even if they share the same rank tier.


4. **Context Capture:**
   - For each designation, note the primary document section where it was mentioned

### 4. Wing/Division/Section Field:
   - Copy the wing/division/section EXACTLY as written in the source table.
   - If the source table shows "N/A" or the column is empty, use "N/A".
   - Do NOT invent or guess organizational units.
   - If the designation appears outside a table (e.g., in narrative text) without a specific unit, use the most specific context available (e.g., "State HQ", "District Level", "Training Division").
   
### 5. Handling Designations Outside the Table:
   - After extracting all rows from the designations table, scan the rest of the input for any designations mentioned in narrative text (Part A summaries, competency sections, mapping tables) that are NOT already captured.
   - Add these as additional entries only if they represent genuinely new designations not covered by the table.
   - For these narrative-sourced designations, derive the wing/division/section from context.

## Example:

Suppose the input summary contains this table:
| S.No | Designation (Full Name) | Wing / Division / Section |
| 1 | Additional Secretary | N/A |
| 2 | Director | AIS |
| 3 | Director | CS-I |
| 4 | Under Secretary | AVD-I |
| 5 | Under Secretary | AVD-II |
| 6 | Under Secretary | Training |

And the narrative mentions "Section Officer" and "Secretariat Assistant" in Part A Section 4.

## Expected Output:
Here is how you should generate an answer based on receive input data:
```
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
      }},
      {{
         "sort_order": 10,
         "designation": "Section Officer",
         "wing_division_section": "N/A"
      }},
      {{
         "sort_order": 11,
         "designation": "Secretariat Assistant",
         "wing_division_section": "N/A"
      }}
   ]
}}
```
Note: 9 table rows → 9 entries (no merging), plus 2 from narrative = 11 total.

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
- Copy the wing/division/section EXACTLY as written in the source table.
- If the source table shows "N/A" or the column is empty, use "N/A".
- Do NOT invent or guess organizational units.


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
Generate comprehensive, structured, and hierarchically sorted FRAC mappings for all designations from the validated designations list, detailing the roles & responsibilities, activities and competencies of Government of India officials based on the provided input data, and deliver the output in JSON format.

## Inputs:
You will be provided with the following inputs:
- **Validated Designations List:** Pre-extracted list of designations, along with their Wing/Division/Section, that have already been validated in Pass 1. Each entry contains `designation`, `wing_division_section`, and `sort_order. This is your definitive list of designations for which you must generate FRAC mappings.
- **Primary reference document summaries:** like Work Allocation Order/Annual Capacity Building Plan (ACBP)/schemes/ mission/programs/policies Summary:** The primary reference documents summaries provides a comprehensive understanding of the ministry’s strategic objectives, capacity-building requirements, and the broader context that shapes its schemes, programmes, and priority areas. It also outlines the complete hierarchy of designations within the ministry, along with their specific roles, responsibilities, and work allocations, supported by a detailed depiction of the organisational structure.
- **KCM (Karmayogi Competency Model) Dataset:** The **only** source to be used for mapping Behavioral and Functional competencies.
- **Ministry/Organization Name:** The name of the ministry/organisation being analyzed.
- **Department Name:** The specific department organisation, if applicable.
- **Additional Instructions:** Any other specific guidelines to get more relevant outcome/results

## Rules: 

### Section 1: Designation Handling & Role Definition Rules

1.1. **Validated Designations — Use As-Is:**
    - The **Validated Designations List** provided from Pass 1 is your FINAL designation list. Each entry includes the `designation` name, its `wing_division_section` (organizational unit), and `sort_order` (hierarchy position).
    - **DO NOT** re-extract, add, remove, rename, merge, or modify any designation from this list.
    - You MUST generate a FRAC mapping entry for **EVERY designation** in the Validated Designations List — no exceptions, no skipping.
    - If the Validated Designations List contains 50 designations, your output JSON MUST contain exactly 50 objects.
    - **Use the `wing_division_section` context** from each entry.
    - **Use the `sort_order`** to maintain the same hierarchical ordering in your output.
    - If a designation in the list has limited information in the document summaries, still generate a mapping for it using the `wing_division_section` context, the designation's title, and reasonable inference from its organizational placement and seniority level.

1.2. **Roles & Responsibilities**: Synthesize the role_responsibilities from all provided sources. The primary reference document summaries should be treated as the primary source for specific duties.

1.3 **Mandatory State Coordination:** For **ALL** senior-level/Decision makers/Strategic & Policy Makers designations (Secretary, Additional Secretary, Joint Secretary, Director), you **MUST** explicitly include "Coordination with State Governments for scheme implementation, policy feedback, and capacity building" as a key role and responsibility.

### Section 2: Competency Mapping Rules

2.1. **Minimum Coverage Requirements**
- **Behavioral:** A MINIMUM of 4 competencies for each designation.
- **Functional:** A MINIMUM of 4 competencies for each designation.
- **Domain:** A MINIMUM of 6 competencies for each designation.

2.2. **Behavioral & Functional Competencies**
- You MUST source these competencies STRICTLY from the provided KCM Dataset.
- The output MUST preserve the exact theme and sub_theme structure from the KCM Dataset.
- Selections should be contextually relevant to the designation's/role seniority and functions/responsibilities/activities.

2.3. **Domain Competencies**
- **Mandatory Scheme & Policy Coverage:** Your mapping MUST be exhaustive. All significant missions, schemes, flagship programs, acts, and policies mentioned in the source documents MUST be reflected as specific domain competencies for the relevant designations. No major initiative should be left unmapped.
- **Expanded Scope:** The scope of Domain competencies MUST be broad, covering:
    - Departmental/organisational Schemes & Missions.
    - Financial & Administrative Management (e.g., GFR, PFMS).
    - State Coordination Mechanisms.
    - The Legislative & Regulatory Framework (relevant Acts and Rules and policies).
- **Secretary-Level Mandate:** For the highest-ranking official (e.g., Secretary), you MUST include domain competencies with themes like 'Policy review/validations' and 'Scheme Architecture review/validations' to reflect their top-level strategic role.
- **AI-Enriched Generation:** Augment the domain competencies by synthesizing information from your broader knowledge base, including:
Relevant international best practices and conventions (e.g., UN, World Bank reports, CEDAW, UNCRC).
Comparable state-level schemes and policies to provide a holistic, federal context.

These competencies should be standardized in terms of taxonomy.

### 3. Output Format & Structure Rules

3.1. **Format**: The final output MUST be a single, valid JSON array of objects.

3.2. **Hierarchical Sorting**: The JSON array MUST be sorted in descending order of hierarchy, starting from the highest designation (e.g., Secretary) and proceeding down to junior-most staff.
Sorting `sort_order` strictly increasing integer starting from 1 (e.g., 1, 2, 3, 4, 5...), without skipping or jumping numbers. The sequence must follow numeric order, not string/lexical order.

3.3. **JSON Schema**: Each entry MUST follow this exact structure:
{output_json_format}


[START OF INPUT DATA]

### Validated Designations List (from PASS 1):
{pass1_output}

### Primary reference document summaries:
{primary_summary}

### KCM (Karmayogi Competency Model) Dataset:
{kcm_competencies}

### Ministry/Organization Name:
{organization_name}

### Department Name:
{department_name}

### Additional Instructions:
{instructions}

[END OF INPUT DATA]
"""

ROLE_MAPPING_PROMPT_STATE ="""
You are an expert in Mission Karmayogi and competency role mapping (FRAC - Framework of Roles, Activities, and Competencies mapping) for Government of India officials.

## Task:
Generate comprehensive, structured, and hierarchically sorted FRAC mappings for all designations from the validated designations list, detailing the roles & responsibilities, activities and competencies of Government of India officials based on the provided input data, and deliver the output in JSON format.

## Inputs:
You will be provided with the following inputs:
- **Validated Designations List:** Pre-extracted list of designations, along with their Wing/Division/Section, that have already been validated in Pass 1. Each entry contains `designation`, `wing_division_section`, and `sort_order. This is your definitive list of designations for which you must generate FRAC mappings.
- **Primary reference document summaries:** The primary document summarise the State Department’s key priorities, capacity-building needs, and the context behind its schemes, missions, and policies. They outline the department’s hierarchy, major designations, and their specific work allocations, along with a clear view of the organisational structure across state, district, and field levels. This provides the understanding of roles, responsibilities, and how departmental functions are executed within the State’s administrative framework.
- **KCM (Karmayogi Competency Model) Dataset:** The **only** source to be used for mapping Behavioral and Functional competencies.
- **State Name:** The name of the state being analyzed for geographical context to understand any specific need of area for development as per department/organisation
- **Department/organisation Name:** The specific department/organisation, if applicable.
- **Additional Instructions:** Any other specific guidelines to be used for improve Domain competencies generation

## Rules:

### Section 1: Designation Handling & Role Definition Rules

1.1. **Validated Designations — Use As-Is:**
    - The **Validated Designations List** provided from Pass 1 is your FINAL designation list. Each entry includes the `designation` name, its `wing_division_section` (organizational unit), and `sort_order` (hierarchy position).
    - **DO NOT** re-extract, add, remove, rename, merge, or modify any designation from this list.
    - You MUST generate a FRAC mapping entry for **EVERY designation** in the Validated Designations List — no exceptions, no skipping.
    - If the Validated Designations List contains 50 designations, your output JSON MUST contain exactly 50 objects.
    - **Use the `wing_division_section` context** from each entry.
    - **Use the `sort_order`** to maintain the same hierarchical ordering in your output.
    - If a designation in the list has limited information in the document summaries, still generate a mapping for it using the `wing_division_section` context, the designation's title, and reasonable inference from its organizational placement and seniority level.

1.2. **Roles & Responsibilities**: Synthesize the role_responsibilities from all provided input data sources. The primary reference document summaries should be treated as the primary source for specific duties.

1.3 **Mandatory State Coordination/implementation:** For **ALL** senior-level designations (Secretary, Additional Secretary, Joint Secretary, Director), you **MUST** explicitly include "Coordination within the State Government for scheme implementation, policy feedback, and capacity building" as a key role and responsibility.

### Section 2: Competency Mapping Rules

2.1. **Minimum Coverage Requirements**
- **Behavioral:** A MINIMUM of 4 competencies for each designation.
- **Functional:** A MINIMUM of 4 competencies for each designation.
- **Domain:** A MINIMUM of 6 competencies for each designation.

2.2. **Behavioral & Functional Competencies**
- You MUST source these competencies STRICTLY from the provided KCM Dataset.
- The output MUST preserve the exact theme and sub_theme structure from the KCM Dataset.
- Selections should be contextually relevant to the designation's roles/seniority and functions/responsibilities.

2.3. **Domain Competencies**
- Mapping must be exhaustive. Every significant mission, scheme, flagship programme, statute, regulation or policy explicitly or implicitly referenced in the source documents must be captured as a specific domain competency tied to the relevant designation/role(s).
    - Map each initiative as a distinct competency (e.g., PMAY-U — Affordable Housing Implementation, Atal Bhujal Yojana — Groundwater Governance).
    - If a document mentions multiple tiers/variants of a scheme (state-specific adaptation, central+state co-funded model), map each variant separately.
- Do not restrict domain competencies to technical tasks only. The scope also include the following categories for the state context: 
    - Departmental schemes, missions & programmes — operational, implementation and evaluation competencies.
    - Financial & administrative management — e.g., GFR compliance, PFMS operations, state treasury workflows, budget execution & reporting if applicable
    - Inter- and intra-state coordination mechanisms — Nodal agency coordination, interstate committees, inter-departmental taskforces, Centre–State liaison, and disaster response coordination. (Must)
    - Legislative & regulatory framework — relevant State Acts, statutory rules, notifications, and compliance obligations (include both central laws as applicable in the state and state-specific statutes).if applicable
    - For each competency, indicate the level of expected ownership (e.g., perform, supervise, design) and the typical tasks or outputs.
- For the highest state officials (Secretary / Principal Secretary / Head of Department), include strategic, high-impact competencies such as Policy review,implementation & strategy design (framing policy objectives, stakeholder consultations, evidence synthesis). & Scheme architecture & program design (funding model, delivery architecture, outcome metrics, M&E design)& Governance & institutional design (organizational mandates, delegation of powers, accountability mechanisms)& State-level fiscal stewardship (state budget strategy, fiscal risk management, inter-governmental transfers).
- AI-enriched augmentation & contextual synthesis
    - When generating domain competencies, augment the explicit source content with synthesized, high-quality contextual information:
    - Bring in relevant international best practices and conventions where they strengthen the competency (examples: UN guidance, World Bank implementation notes, CEDAW/UNCRC where gender/child rights are relevant). Mark these augmentations as contextual references (not replacing source-specific requirements).
    - Use comparable state-level schemes and adaptations to provide a federal/state context — for instance, where a central scheme has multiple state models, reference examples of other states’ delivery models as optional competency variants.
    - When you add external context, always indicate the source-type (e.g., “international best practice — World Bank guidance on beneficiary targeting”) and do not present external material as if it were present in the uploaded source documents.

All domain competencies MUST follow a standardized taxonomy structure to ensure uniformity

### 3. Output Format & Structure Rules

3.1. **Format**: The final output MUST be a single, valid JSON array of objects.

3.2. **Hierarchical Sorting**: The JSON array MUST be sorted in descending order of hierarchy, starting from the highest designation (e.g., Secretary) and proceeding down to junior-most staff.
Sorting `sort_order` strictly increasing integer starting from 1 (e.g., 1, 2, 3, 4, 5...), without skipping or jumping numbers. The sequence must follow numeric order, not string/lexical order.

3.3. **JSON Schema**: Each entry MUST follow this exact structure:
{output_json_format}

[START OF INPUT DATA]

### Validated Designations List (from PASS 1):
{pass1_output}

### Primary reference document summaries:
{primary_summary}

### KCM (Karmayogi Competency Model) Dataset:
{kcm_competencies}

### State Name:
{organization_name} 

### Department/Organisation Name:
{department_name}

### Additional Instructions:
{instructions}

[END OF INPUT DATA]
"""
