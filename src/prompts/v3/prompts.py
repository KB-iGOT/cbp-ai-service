DESIGNATION_EXTRACTION_PROMPT = """
You are an expert in analyzing Government of India organizational structures and extracting designation hierarchies.

## Task:
Extract ALL unique designations from the provided input data and organize them hierarchically.

## Inputs:
- **Primary reference document Summaries:**
- **Ministry/State Name:**
- **Department/Organisation Name:**

## CRITICAL CONSTRAINTS:

### Strict Data Boundary Rules:
1. **ONLY use designations explicitly mentioned in the input data provided**
2. **DO NOT invent, assume, or add any designations from:**
   - Your general knowledge
   - Standard government hierarchies
   - Other ministries/departments
   - External references or documentation
   - Common administrative positions not mentioned in input

3. **If a designation is not in the input data, DO NOT include it - even if it seems logical or standard**

## Extraction Rules:

1. **Comprehensive Coverage:**
   - Extract EVERY designation mentioned across all input sources
   - Copy designations EXACTLY as written in the source documents
   - Do not include variations (e.g., "Joint Secretary" vs "Jt. Secretary")
   - DO NOT standardize or normalize unless the same designation appears in clearly identical contexts

2. **Hierarchical Classification:**
   - Organize designations from highest to lowest seniority
   - Group by administrative levels: State HQ → District → Block → Field
   - Assign a preliminary hierarchy score (sort_order: 1 = highest)

3. **Mandatory Verification Step:**
   - Re-scan all inputs after initial extraction
   - Create a checklist confirming each source document has been covered
   - Flag any ambiguous or unclear designations for review

4. **Context Capture:**
   - For each designation, note the primary document section where it was mentioned
   - Capture brief context about the role (1-2 sentences max)
   - Identify reporting relationships if explicitly stated

## Output Format (JSON):

{output_format}

## Verification Requirements:
Before finalizing, confirm:
- Every section of input documents has been reviewed
- No designation appears multiple times in final list
- Hierarchy ordering is logical and complete
- All variations/synonyms are captured under normalized names

[START OF INPUT DATA]
### Primary reference document Summaries:
{primary_summary}

### Ministry/State Name:
{organization_name}

### Department/Organisation Name:
{department_name}

[END OF INPUT DATA]
"""

DESIGNATION_EXTRACTION_PROMPT_UPDATED = """
You are an expert in analyzing Government of India organizational structures and extracting designation hierarchies.

## Task:
Extract ALL unique designations EXACTLY as mentioned in the provided input data and organize them hierarchically.

## Inputs:
- **Primary reference document Summaries:**
- **Ministry/State Name:**
- **Department/Organisation Name:**

## CRITICAL CONSTRAINTS:

### Strict Data Boundary Rules:
1. **ONLY use designations explicitly mentioned in the input data provided**
2. **DO NOT invent, assume, or add any designations from:**
   - Your general knowledge
   - Standard government hierarchies
   - Other ministries/departments
   - External references or documentation
   - Common administrative positions not mentioned in input

3. **If a designation is not in the input data, DO NOT include it - even if it seems logical or standard**

## Extraction Rules:

1. **Exact Designation Capture:**
   - Copy designations EXACTLY as written in the source documents
   - Preserve original spelling, capitalization, and formatting
   - If a designation appears in multiple forms (e.g., "Joint Secretary" vs "Jt. Secretary"), list all variations
   - Mark which variation is most frequently used in the source
   - DO NOT standardize or normalize unless the same designation appears in clearly identical contexts

2. **Comprehensive Coverage:**
   - Extract EVERY designation mentioned across all input sources
   - Scan each document section systematically
   - Include both permanent and contractual positions if explicitly mentioned
   - Include acting/additional charge positions if stated

3. **Hierarchical Classification:**
   - Organize designations from highest to lowest seniority BASED ONLY on:
     * Explicit hierarchy statements in the input data
     * Reporting relationships mentioned in the documents
     * Organizational charts if provided
   - DO NOT assume hierarchy based on external knowledge
   - If hierarchy is unclear from input data, mark as "hierarchy uncertain"
   - Group by administrative levels ONLY if this structure is evident in the input
   - Assign a preliminary hierarchy score (sort_order: 1 = highest)

4. **Mandatory Verification Steps:**
   BEFORE finalizing extraction, complete this checklist:
   
   a) **Document Coverage Check:**
      - [ ] Every section of primary reference document has been reviewed
      - [ ] All mentions of designations have been captured
      - [ ] Cross-referenced all input sources for completeness
   
   b) **Accuracy Verification:**
      - [ ] Each designation copied exactly as it appears in source
      - [ ] No designations added from external knowledge
      - [ ] All variations of same designation are noted
      - [ ] Source location documented for each designation
   
   c) **Duplication Check:**
      - [ ] No designation appears multiple times in final list
      - [ ] Variations clearly marked and linked
      - [ ] Merged only when contextually identical
   
   d) **Hierarchy Validation:**
      - [ ] Hierarchy based solely on input data evidence
      - [ ] Uncertain hierarchies clearly flagged
      - [ ] No assumptions made from standard government structures

5. **Context Capture:**
   - For each designation, note:
     * Exact document section/page where mentioned
     * Brief context about the role (1-2 sentences max, ONLY from input data)
     * Reporting relationships if explicitly stated in documents
     * Frequency of mention across documents
   - Mark designations with insufficient context as "requires clarification"

6. **Quality Assurance Instructions:**
   - After extraction, re-read the input data completely
   - Verify each extracted designation against source
   - Flag any designation you're uncertain about
   - If input data is ambiguous or incomplete, state this clearly
   - DO NOT fill gaps with assumptions

## Output Format (JSON):

{output_format}

## Additional Output Requirements:

Include a verification section:
```json
{
  "verification": {
    "total_designations_extracted": <number>,
    "sources_reviewed": [<list of document sections>],
    "variations_found": [<list of designation variations>],
    "hierarchy_confidence": "<high/medium/low>",
    "missing_information": [<list of gaps in input data>],
    "assumptions_made": "NONE - All data from input only",
    "external_references_used": "NONE"
  }
}
```

## Final Verification Requirements:
Before submitting, confirm:
- ✓ Every designation is directly traceable to input data
- ✓ Zero designations added from external knowledge
- ✓ Exact text matching between extraction and source
- ✓ All document sections reviewed and documented
- ✓ Hierarchy based only on explicit input data evidence
- ✓ Variations and duplicates properly handled
- ✓ Uncertain elements clearly flagged

## What NOT to do:
❌ Add common government positions not in the input
❌ Assume standard hierarchies from general knowledge
❌ Normalize designations without source evidence
❌ Fill missing information from external sources
❌ Invent reporting structures not stated in documents
❌ Use abbreviations not present in original text

[START OF INPUT DATA]
### Primary reference document Summaries:
{primary_summary}

### Ministry/State Name:
{organization_name}

### Department/Organisation Name:
{department_name}

[END OF INPUT DATA]

**REMINDER: Extract ONLY from the input data above. No external knowledge. No assumptions. Exact designations only.**
"""

ROLE_MAPPING_PROMPT_CENTRE_V3 ="""
You are an expert in Mission Karmayogi and competency role mapping (FRAC - Framework of Roles, Activities, and Competencies mapping) for Government of India officials.

## Task:
Generate comprehensive, structured, and hierarchically sorted FRAC mappings for all designations from the validated extraction list, detailing the roles, responsibilities, and competencies of Government of India officials based on the provided input data, and deliver the output in JSON format.

## Inputs:
You will be provided with the following inputs:
- **Validated Designations List:** Extracted list of desigations
- **Primary reference document summaries:** like Work Allocation Order/Annual Capacity Building Plan (ACBP)/schemes/ mission/programs/policies Summary:** The primary reference documents summaries provides a comprehensive understanding of the ministry’s strategic objectives, capacity-building requirements, and the broader context that shapes its schemes, programmes, and priority areas. It also outlines the complete hierarchy of designations within the ministry, along with their specific roles, responsibilities, and work allocations, supported by a detailed depiction of the organisational structure.
- **KCM (Karmayogi Competency Model) Dataset:** The **only** source to be used for mapping Behavioral and Functional competencies.
- **Ministry/Organization Name:** The name of the ministry/organisation being analyzed.
- **Department Name:** The specific department organisation, if applicable.
- **Organisation Name: (Optional)** The specific organisation, if applicable.
- **Additional Instructions:** Any other specific guidelines to get more relevant outcome/results

## Rules: 

### Section 1: Data Extraction & Role Definition Rules

1.1. **Exhaustive Designation Extraction**: 
    - You **MUST** extract all unique designations from the provided primary reference document summaries input data. Merge and deduplicate any overlaps.
    - **Explicit List Handling:** If the "Primary reference document summaries" contain a section labeled **"List of Designations"**, **"Part B: Detailed Lists"**, or similar, you must extract **EVERY SINGLE ENTRY** from that list.
    - **Zero Truncation Policy:** Do not summarize, sample, or skip designations. If the list contains 50 designations, your output JSON must contain 50 objects.
    - Do not miss any designations which are mentioned in the all primary reference document summaries.
    - Before finalizing the response, you MUST perform a verification step: Re-scan all inputs and confirm: Every designation mentioned in all the primary reference document summaries appears in the extracted list.

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

These competencies Should be standardarise in terms of taxonomy.

### 3. Output Format & Structure Rules

3.1. **Format**: The final output MUST be a single, valid JSON array of objects.

3.2. **Hierarchical Sorting**: The JSON array MUST be sorted in descending order of hierarchy, starting from the highest designation (e.g., Secretary) and proceeding down to junior-most staff.
Sorting `sort_order` strictly increasing integer starting from 1 (e.g., 1, 2, 3, 4, 5...), without skipping or jumping numbers. The sequence must follow numeric order, not string/lexical order.

3.3. **JSON Schema**: Each entry MUST follow this exact structure:
{output_json_format}


[START OF INPUT DATA]

### Validated Designations (from PASS 1):
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

ROLE_MAPPING_PROMPT_STATE_V3 ="""
You are an expert in Mission Karmayogi and competency role mapping (FRAC - Framework of Roles, Activities, and Competencies mapping) for Government of India officials.

## Task:
Generate comprehensive, structured, and hierarchically sorted FRAC mappings for all designations from the validated extraction list, detailing the roles, responsibilities, and competencies of Government of India officials based on the provided input data, and deliver the output in JSON format.

## Inputs:
You will be provided with the following inputs:
- **Validated Designations List:** Extracted list of desigations
- **Primary reference document summaries:** The primary document summarise the State Department’s key priorities, capacity-building needs, and the context behind its schemes, missions, and policies. They outline the department’s hierarchy, major designations, and their specific work allocations, along with a clear view of the organisational structure across state, district, and field levels. This provides the understanding of roles, responsibilities, and how departmental functions are executed within the State’s administrative framework.
- **KCM (Karmayogi Competency Model) Dataset:** The **only** source to be used for mapping Behavioral and Functional competencies.
- **State Name:** The name of the state being analyzed for geographical context to understand any specific need of area for development as per department/organisation
- **Department/organisation Name:** The specific department/organisation, if applicable.
- **Additional Instructions:** Any other specific guidelines to be used for improve Domain competencies generation

## Rules:

### Section 1: Data Extraction & Role Definition Rules

1.1. **Exhaustive Designation Extraction**: 
    - You **MUST** extract all unique designations from the provided primary reference document summaries input data. Merge and deduplicate any overlaps.
    - **Explicit List Handling:** If the "Primary reference document summaries" contain a section labeled **"List of Designations"**, **"Part B: Detailed Lists"**, or similar, you must extract **EVERY SINGLE ENTRY** from that list.
    - **Zero Truncation Policy:** Do not summarize, sample, or skip designations. If the list contains 50 designations, your output JSON must contain 50 objects.
    - Do not miss any designations which are mentioned in the all primary reference document summaries.
    - Before finalizing the response, you MUST perform a verification step: Re-scan all inputs and confirm: Every designation mentioned in all the primary reference document summaries appears in the extracted list.

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

### Validated Designations (from PASS 1):
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
