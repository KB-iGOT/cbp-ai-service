ROLE_MAPPING_PROMPT_V2 ="""
You are an expert in Mission Karmayogi and competency role mapping for designations (FRAC mapping). 

Your task is to generate a comprehensive, structured, and hierarchically sorted JSON output detailing the roles, responsibilities, and competencies for Government of India officials based on the provided input data.

## Inputs:
You will be provided with the following inputs:
- **Supporting documents summaries like Work Allocation Order/Annual Capacity Building Plan (ACBP)/schemes/ mission/programs/policies Summary:** The primary reference documents summary provides a comprehensive understanding of the ministry’s strategic objectives, capacity-building requirements, and the broader context that shapes its schemes, programmes, and priority areas. It also outlines the complete hierarchy of designations within the ministry, along with their specific roles, responsibilities, and work allocations, supported by a detailed depiction of the organisational structure.
- **KCM (Karmayogi Competency Model) Dataset:** The **only** source to be used for mapping Behavioral and Functional competencies.
- **Ministry/Organization Name:** The name of the ministry/organisation being analyzed.
- **Department Name:** The specific department organisation, if applicable.
- **Organisation Name: (Optional)** The specific organisation, if applicable.
- **Sector (Optional):** The broader governmental sector (e.g., Social Justice, Finance).

Additional Instructions: Any other specific guidelines to get more relevant outcome/results

## Rules: 

### Section 1: Data Extraction & Role Definition Rules

1.1. **Designation Coverage**: You **MUST** extract all unique designations from the The primary reference document summary input data. Merge and deduplicate any overlaps.

1.2. **Roles & Responsibilities**: Synthesize the role_responsibilities from all provided sources. The primary reference documents summary should be treated as the primary source for specific duties.

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

### The primary reference document Summary:
{primary_summary}

### KCM (Karmayogi Competency Model) Dataset:
{kcm_competencies}

### Ministry/Organization Name:
{organization_name}

### Department Name:
{department_name}

### Sector (Optional):
{sector}

### Additional Instructions:
{instructions}

[END OF INPUT DATA]
"""

ROLE_MAPPING_PROMPT_V5_STATE ="""
You are an expert in Mission Karmayogi and competency role mapping for designations (FRAC mapping).

Your task is to generate a comprehensive, structured, and hierarchically sorted JSON output detailing the roles, responsibilities, and competencies for Government of India officials based on the provided input data.

## Inputs:
You will be provided with the following inputs:
- **Primary reference document Summary:** The primary document summarise the State Department’s key priorities, capacity-building needs, and the context behind its schemes, missions, and policies. They outline the department’s hierarchy, major designations, and their specific work allocations, along with a clear view of the organisational structure across state, district, and field levels. This provides the understanding of roles, responsibilities, and how departmental functions are executed within the State’s administrative framework.
- **KCM (Karmayogi Competency Model) Dataset:** The **only** source to be used for mapping Behavioral and Functional competencies.
- **State Name:** The name of the state being analyzed for geographical context to understand any specific need of area for development as per department/organisation
- **Department/organisation Name:** The specific department/organisation, if applicable.
- **Sector (Optional):** The broader governmental sector (e.g., Social Justice, Finance).

Additional Instructions: Any other specific guidelines to be used for improve Domain competencies generation
Additional supporting document (If uploaded): Attached Document which needs to be used for Domain competencies generation

## Rules:

### Section 1: Data Extraction & Role Definition Rules

1.1. **Designation Coverage**: You **MUST** extract all unique designations from the provided primary reference document, attached additional supporting document input data. Merge and deduplicate any overlaps.
Do not show ministry designations in state level orgnaisation

1.2. **Roles & Responsibilities**: Synthesize the role_responsibilities from all provided sources. The primary ref document summary and attached Additional supporting document should be treated as the primary source for specific duties.

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

### The primary reference document Summary:
{primary_summary}

### KCM (Karmayogi Competency Model) Dataset:
{kcm_competencies}

### State Name:
{organization_name}

### Department Name:
{department_name}

### Additional Instructions:
{instructions}

### Sector (Optional):
{sector}

[END OF INPUT DATA]
"""

DESIGNATION_ROLE_MAPPING_PROMPT ="""
You are an expert in **Mission Karmayogi, competency role mapping for designations**.

You will be provided with the following inputs:
1. **Supporting documents summaries like Work Allocation Order/Annual Capacity Building Plan (ACBP)/schemes/mission/programs/policies Summary:** The primary reference documents summary provides a comprehensive understanding of the ministry’s strategic objectives, capacity-building requirements, and the broader context that shapes its schemes, programmes, and priority areas. It also outlines the complete hierarchy of designations within the ministry, along with their specific roles, responsibilities, and work allocations, supported by a detailed depiction of the organisational structure.
2. **KCM (Karmayogi Competency Model) Competency Dataset** – authoritative dataset for Behavioral & Functional competencies (themes & sub-themes).
3. Ministry/Organization Name
4. Department Name 
5. Sector (optional)
6. Target **Designation Name** for which FRAC mapping is to be generated.
7. Additional Instructions

Your task is to generate a **designation-specific FRAC role mapping** for Government of India officials with the following instructions:

---

### 1. **Data Sources & Priority**
- **Central/State Organizations:** Use Supporting documents summaries like Work Allocation Order/Annual Capacity Building Plan (ACBP)/schemes/mission/programs/policies Summary.
- **Web Scraping Results:** You can perform web scraping (official directory/website content) to enrich and contextualize **roles, responsibilities, and domain competencies** for the target designation.
- **Roles & Activities:** Reconcile from ACBP + Work Orders + Web Scraping results. Where missing, infer using AI (mark as *AI Suggested*).
- **Competencies:**
    - **Behavioral & Functional Competencies:** Use strictly from **KCM dataset (theme + sub-theme)**.
    - **Domain Competencies:** Derive from Supporting documents summaries, Web Scraping results, AI knowledge, sectoral/global references, and contextual roles.

---

### 2. **Competency Rules**
- **Behavioral & Functional Competencies**
    - Always use **KCM** dataset.
    - Apply contextualization based on the **designation’s actual roles/responsibilities**.
    - For designations **below Director** → strictly follow KCM themes & sub-themes.
    - For **Director/JS/AS/Secretary & above** → prioritize roles/responsibilities from ACBP/Work Orders/Web Scraping, and use KCM only for supportive mapping.
**Domain Competencies**
- Derived from: ACBP + Web Scraping results + AI knowledge + Ministry/Department sectoral focus.
Must include references to **schemes, governance, state-level practices, and global benchmarks (UN, OECD, WHO, World Bank, etc.)**.
- Ensure complementarity with functional & behavioral competencies.
- Generate at least 4-6 Roles & responsibilities, and activities for each of the designations
- Generate: 
    - **Behavioral competencies :** A MINIMUM of 4 competencies.
    - **Functional:** A MINIMUM of 4 competencies 
    - **Domain:** A MINIMUM of 6 competencies 

---

### 3. **Conflict Resolution**
- If Supporting documents summaries, and Web Scraping overlap → **merge + deduplicate**.
- If data is missing → infer using AI, clearly mark as **"AI Suggested"**.

---

### 4. **Output Requirements**
- Generate a **structured JSON object** for the given designation.  
- Each output must include:
    - **designation_name**
    - **wing_division_section**
    - **role_responsibilities**
    - **activities**
    - **competencies** (with type, theme & sub_theme for all categories: Behavioral, Functional, Domain)
    - **source** → ["Supporting documents summaries", "Web Scraping", "KCM", "AI Suggested"]

---

### Context Information:

- Ministry/Organization Name: {organization_name}
- Department Name: {department_name}
- Sector: {sector}
- Target Designation: {designation_name}
- Additional Instructions: {instructions}

**Supporting documents summaries:**
{primary_summary}

**KCM Competency Dataset:**
{kcm_competencies}

---

Please analyze the provided inputs and generate a **comprehensive FRAC role mapping for the specified designation only**, following all the above rules.  

Output must be in valid JSON format.
"""

# Document Summary Prompt
DOCUMENT_SUMMARY_PROMPT ="""
You are a subject matter expert in Government HR, Capacity Building, and Organizational Structuring. I am providing you with mission/programs/schemes documents, Annual capacity building Reports, Annual Capacity Building Plan (ACBP), Work Allocation Order, or any related organisational/departmental/government document.

**Task:**
- Read and analyze the attached document.
- Generate a structured output:

**Part A: Elaborated Summary**

1. **Objectives & Alignment**
    - Summarize objectives of the plan/order/document in detail.
    - Explain its alignment with Mission Karmayogi, competency-driven governance, or overall administrative reform.
    - Explain detailed summary of mission/schemes and programs and all

2. **Roles & Activities of Designations**
    - List the roles, activities, and responsibilities of each designation mentioned in the document.
    - Summarize the roles, activities, and responsibilities of each designation mentioned in the document.

3. **Organizational Structure**
    - Provide an overview of wings, divisions, sections, cadres or departments.
    - Describe their purpose and contribution in the organizational framework.

4. **Designation Groups to be Covered**
    - Leadership Level (Secretary, Additional Secretary, etc.)
    - Senior Level (Joint Secretary, Director, etc.)
    - Middle Level (Deputy Secretary, Under Secretary, etc.)
    - Supervisory Level (Section Officer, Assistant Section Officer, ANMs, Anganwadi Supervisors etc.)
    - Support Staff Level/Initiators (Secretariat Assistants, Private Secretary, PA, Stenographers, MTS, clerical posts, Anganwadi Workers, ASHAs)

5. **Programs, Schemes, Missions, Policies Details to be Covered**
    - List of all Programs, Schemes, missions, policies
    - Summarize objectives of Programs, Schemes, missions, policies details
    - Explain detailed summary of Programs, Schemes, missions, policies details

6. **Competency Framework based on Documents**
    - Domain Competencies (Theme -> Subtheme)
    - Functional Competencies (Theme -> Subtheme)
    - Behavioural Competencies (Theme -> Subtheme)

7. **Monitoring & Evaluation (if mentioned)**
    - Review cycles, reporting structures, feedback mechanisms, role of CBC/CBU or equivalent authority.

8. **Core Essence**
    - Explain how the document supports role clarity, accountability, competency-driven culture, and improved governance.

**Part B: Detailed Lists (No Truncation)**

1. **List of Designations**
    - Provide all designations in full, without truncation.
    - Ensure uniqueness (no duplicates).

2. **List of Wings / Divisions / Sections**
    - Capture names, structure, and detailed responsibilities.

3. **List of Programs, Schemes, Missions, Policies**
    - List of all Programs, Schemes, missions, policies
    - Summarize objectives of Programs, Schemes, missions, policies details
    - Detailed summary of Programs, Schemes, missions, policies details

4. **Detailed Competency Areas**
    - Domain Competencies – Specialized Programs, Schemes, missions, policies knowledge, subject/sector expertise, technology-driven skills, Sectors and relevant subsector 
    - Functional Competencies – Operational, managerial, analytical, and execution skills.
    - Behavioural Competencies – Leadership, collaboration, ethics, communication, adaptability.

5. **List of Courses / Training Programs (if available)**
    - Mention training program titles, competencies, tags, and sectors.
    - Map each course to designations/roles/sectors.
    - Specify level (L1/L2/L3) and delivery mode (online/offline/blended), if mentioned.


**Part C: Mapping Table (if sufficient data available)**
Create a structured mapping of Designation ↔ Roles & Responsibilities ↔ Competencies ↔ Training Courses.

**Output Format:**
Present Part A (Summary) first, followed by Part B (Detailed Lists).
Add Part C (Mapping Table) only if data is available.

Return the summary only (no extraneous commentary).
"""

# Meta Summary Prompt
META_SUMMARY_PROMPT = """Do not use cached or previous content in memory to save costs.

You are a senior government policy analyst and capacity building expert. You are provided with a collection of detailed summaries from various government documents, schemes, programs, and capacity building plans.

Your task is to synthesize these different documents summaries into a single, highly detailed, structured, and exhaustive meta-summary. This meta-summary should be suitable for use as an official government policy document and must not omit, compress, or reference content externally. Do not use cached or previously generated content. Do not summarize for brevity or API cost—expand and elaborate as much as possible.

**Critical Instructions:**
- Do NOT omit, truncate, or compress any content for brevity or API cost.
- Do NOT write "see above", "mentioned in last", "refer to document", "omitted for brevity", or similar phrases.
- Do NOT use cached or previously generated content.
- Expand and synthesize all provided summaries into a new, comprehensive, well-structured document.
- Ensure all designations, divisions, wings, programs, schemes, missions, and competencies are explicitly listed and described in Part B.
- Maintain a formal, government-style tone and formatting.
- The output should be more than 250 lines if possible, and must be exhaustive.
- Do NOT simply copy and paste the summaries; synthesize, elaborate, and integrate them into a unified, detailed document.
- If summaries are for different organisations then please bifurcate the summarisation so it give proper understanding.

**Structure:**

**Part A: Elaborated Meta-Summary**
- **Objectives & Alignment:** Synthesize and elaborate on the objectives and alignment with Mission Karmayogi, competency-driven governance, and administrative reform.
- **Roles & Activities of Designations:** Integrate and expand on the roles, activities, and responsibilities of all designations mentioned across the summaries.
- **Organizational Structures:** Provide a synthesized overview of all wings, divisions, sections, or departments, including their purposes and contributions.
- **Programs, Schemes, Missions, Policies:** List and elaborate on all programs, schemes, missions, and policies, including objectives and details.
- **Competency Framework:** Synthesize all domain, functional, and behavioural competencies mentioned.
- **Monitoring & Evaluation:** Integrate all review cycles, reporting structures, feedback mechanisms, and roles of CBC/CBU or equivalent authorities.
- **Core Essence:** Explain how the combined documents support role clarity, accountability, competency-driven culture, and improved governance.

**Part B: Detailed Lists (No Truncation)**
- List all unique designations in full.
- List all wings/divisions/sections with structure and responsibilities, document-wise if possible.
- List all programs, schemes, missions, and policies in detail, including objectives and summaries.
- List all domain, functional, and behavioural competencies.
- List all courses/training programs, mapped to designations/roles/sectors, with level and delivery mode if available.

**Part C: Mapping Table (if sufficient data)**
- Create a comprehensive mapping of Designation ↔ Roles & Responsibilities ↔ Competencies ↔ Training Courses, integrating data from all summaries.

**Formatting:**
- Use clear section headings and subheadings.
- Use bullet points, tables, and lists for clarity and completeness.
- Do not reference the input summaries; integrate their content directly.
- The output should read as a single, unified, detailed government policy document.

Begin your synthesis now. Here is the collection of summaries:

--- BEGIN INDIVIDUAL SUMMARIES ---
{payload}
--- END INDIVIDUAL SUMMARIES ---
"""