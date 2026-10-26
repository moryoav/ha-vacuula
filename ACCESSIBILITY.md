# Accessibility

I aim to make this integration usable through Home Assistant's standard
entities, actions and configuration forms. Controls and selectable values have
text labels. I rely on Home Assistant's frontend for keyboard navigation,
screen reader behavior and visual presentation.

## Known limits

I have not completed a formal accessibility audit. Generated floor maps are
images and do not provide a full text description of room layout or boundaries.
Map metadata can be read with the integration's `get_data` action. English is
currently the only bundled translation.

## Reporting a barrier

Please use the [bug report form](https://github.com/moryoav/ha-vacuula/issues/new?template=bug_report.yml)
and select Accessibility as the affected area. Include the Home Assistant and
integration versions, the control or task involved, and your browser or app and
assistive technology where relevant. Describe what prevents completing the
task and what you expected to happen.

I welcome suggestions and contributions. Remove private device and Home
Assistant information from screenshots, logs and examples before sharing them.
