# FACT
FACT - Formulaic Language Annotation and Classification Tool


![](https://github.com/SGensicke/FACT/blob/main/Docu/FACT_DEMO_Annotating.gif "FACT demonstration")

FACT is developed to annotate formulaic language in medieval charters.
Diplomatic Sections form the basic units for annotation. Within these sections, recurring diplomatic formulae appear as textual realizations of abstract formula types. Individual occurrences in the text are treated as formula Variants, which are linked to their corresponding ideal (archetypal) Formula. The software imports XML documents following a predefined schema. Each XML file may contain multiple charters, which must already be segmented into their diplomatic sections. In our corpus, each XML file represents the charters issued by a single issuer, allowing the documents to be grouped accordingly. This XML structure is reflected in the application's filter categories, such as Region and Section. Annotations are typically performed section by section, although entire charters can also be annotated if desired. Before annotation, users may define ideal formulae in advance or create them incrementally during the annotation process. At the formula level, users can assign Tags that support later classification, visualization, and quantitative analysis of the annotated corpus.

![](https://github.com/SGensicke/FACT/blob/main/Docu/FACT_Annotate.png "FACT Annotation view")

The annotation workflow is designed to be intuitive and efficient, minimizing manual effort while keeping the annotator focused on the source text.
1. Filter the material to annotate using :
- full-text search or xml:id
- region
- diplomatic section
2. Navigate through the selected sections. The section list can be sorted by:
- xml:id
- alphabetical order
- percentage of annotated text
3. Highlight a text passage in the document view to create a formula variant. 
- FACT supports overlapping annotations.
4. Link the highlighted variant to an existing formula by:
a) dragging the selection card onto one of the suggested formulae
b) searching manually or creating a new formula on the fly

![](https://github.com/SGensicke/FACT/blob/main/Docu/FACT_Inspect-01.png "FACT Formula view")

5. The overview page provides two complementary views:
- Formulae & Variants
- Texts.
6. Both views can be filtered by Region, Section, and Tags. A search bar allows users to quickly locate specific formulae or charters.
7. In the Formulae & Variants view, formulae can be selected for export as a CSV file for further analysis. Columns: formula_id, formula_text, formula_tags, variant_id, variant_xmlid, variant_string, offsetStart, offsetEnd.
8. Selecting a formula card opens its detail view, where users can:
- edit the displayed formula text
- manage assigned tags
- inspect and edit all linked variants:
- The list of variants can be sorted either by xml:id or alphabetically by the variant text.
- Each xml:id links directly to the corresponding location in the Texts view, while clicking a variant opens the annotation interface at the respective occurrence.
- Individual variants can be edited directly from this view.
- A bulk edit mode allows one or multiple variants to be reassigned to another existing formula or merged into a newly created one.

9. The Texts view provides a browsing interface for complete charters, allowing users to inspect individual diplomatic sections within their textual context. From each section, a direct link opens the annotation interface, enabling seamless switching between browsing and annotation.

