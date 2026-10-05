import { formatLabel } from "tiny-format";
import { average } from "./math.js";

class ReportBuilder {
  constructor(title) {
    this.title = title;
    this.rows = [];
  }

  addRow(name, values) {
    const mean = average(values);
    this.rows.push(formatLabel(name, mean));
    return this;
  }

  render() {
    return [this.title, ...this.rows].join("\n");
  }
}

export function buildReport(data) {
  const builder = new ReportBuilder("Report");
  for (const [name, values] of Object.entries(data)) {
    builder.addRow(name, values);
  }
  return builder.render();
}

globalThis.buildReport = buildReport;
