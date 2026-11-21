/** Editable academic slides from a source-checked presentation plan. */
import fs from 'node:fs/promises';
import path from 'node:path';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';

const WIDTH = 1280;
const HEIGHT = 720;
const NAVY = '#203B55';
const INK = '#343B43';
const MISSING_TOPICS = {
  title: ['书目信息', 'publication details'], background: ['背景', 'background'],
  problem: ['研究问题', 'research question'], existing_limitations: ['已有方法局限', 'prior limitations'],
  motivation: ['研究动机', 'motivation'], contributions: ['贡献', 'contributions'],
  overview: ['方法架构', 'method architecture'], experimental_setup: ['实验设置', 'experimental setup'],
  main_results: ['主要结果', 'main results'], conclusion: ['结论', 'conclusion'],
};

// Conservative wrapping keeps CJK glyphs and Latin words within the same box.
function weight(character) {
  if (/[^\u0000-\u00ff]/u.test(character)) return 1;
  if (/[WM]/u.test(character)) return 1;
  if (/[A-Zmw]/u.test(character)) return 0.8;
  if (/[\s.,:;!'ilI]/u.test(character)) return 0.32;
  return 0.57;
}

export function wrap(text, capacity) {
  const lines = [];
  for (const paragraph of text.split('\n')) {
    let line = '';
    let used = 0;
    const tokens = paragraph.match(/[A-Za-z0-9][A-Za-z0-9_.:/%+–—-]*|[^A-Za-z0-9]/gu) || [''];
    for (const token of tokens) {
      const pieces = Array.from(token).reduce((sum, character) => sum + weight(character), 0) > capacity
        ? Array.from(token) : [token];
      for (const character of pieces) {
        const next = Array.from(character).reduce((sum, item) => sum + weight(item), 0);
        if (used + next > capacity && line) {
          if (/^[、。，；：！？）】》」』”’]$/u.test(character) && used + next <= capacity + 1) {
            line += character;
            used += next;
            continue;
          }
          lines.push(line.trimEnd());
          line = '';
          used = 0;
        }
        if (!line && !character.trim()) continue;
        line += character;
        used += next;
      }
    }
    lines.push(line.trimEnd());
  }
  return lines;
}

function lineCapacity(width, size) {
  return Math.max(1, Math.floor(width * 0.96 / size) - 1);
}

function fitText(text, width, height, maximumSize, minimumSize) {
  for (let size = maximumSize; size >= minimumSize; size--) {
    const lines = wrap(text, lineCapacity(width, size));
    if (lines.length * Math.ceil(size * 1.25) <= height) return {text: lines.join('\n'), size};
  }
  return null;
}

function addText(slide, text, box, size, color = INK, bold = false, align = 'left') {
  const shape = slide.shapes.add({
    geometry: 'textbox', name: 'editable-text', position: box,
    fill: 'none', line: { fill: 'none', width: 0 },
  });
  shape.text = text;
  shape.text.style = {
    typeface: 'Times New Roman', fontSize: size, color, bold,
    alignment: align, verticalAlignment: 'top', insets: 0, wrap: 'none',
  };
  return shape;
}

function textPages(bullets, {width = 1116, height = 486, continuationHeight = height, size = 28, gap = 24} = {}) {
  const pages = [];
  let page = [];
  let used = 0;
  const lineHeight = Math.ceil(1.28 * size);
  for (const bullet of bullets) {
    const lines = wrap(bullet, lineCapacity(width, size));
    while (lines.length) {
      const spaceBefore = page.length ? gap : 0;
      const pageHeight = pages.length ? continuationHeight : height;
      const remaining = Math.floor((pageHeight - used - spaceBefore - 6) / lineHeight);
      if (remaining < 1 || (page.length && lines.length > remaining)) {
        pages.push(page);
        page = [];
        used = 0;
        continue;
      }
      const group = lines.splice(0, remaining);
      const groupHeight = group.length * lineHeight + 6;
      used += spaceBefore + groupHeight;
      page.push({text: group.join('\n'), height: groupHeight});
    }
  }
  if (page.length || !pages.length) pages.push(page);
  return pages;
}

function addTextGroups(slide, groups, {left = 82, top = 182, width = 1116, size = 28,
  gap = 24, color = INK, alignment = 'left'} = {}) {
  for (const group of groups) {
    addText(slide, group.text, {left, top, width, height: group.height}, size, color, false, alignment);
    top += group.height + gap;
  }
}

function addDisclosure(slide, plan, spec) {
  const english = plan.language === 'en';
  const lines = [];
  if (plan.source_kind === 'abstract') {
    lines.push(english ? 'Source: abstract only. Full text, figures and appendices were not reviewed.'
      : '材料范围：仅公开摘要，未读取全文、图表或附录。');
  }
  const missing = new Set(spec.missing_topics || []);
  if (spec.overview_display === 'missing') missing.add('overview');
  if (missing.size) {
    const labels = [...missing].map(topic => MISSING_TOPICS[topic]?.[english ? 1 : 0] ?? topic);
    lines.push((english ? 'Insufficient evidence: ' : '资料不足：') + labels.join(english ? ', ' : '、'));
  } else if (spec.figure_limitations?.length) {
    lines.push(spec.figure_limitations[0]);
  }
  if (lines.length) {
    const box = {left: 68, top: 678, width: 1144, height: 38};
    const footer = fitText(lines.join('  ·  '), box.width, box.height, 17, 14);
    if (!footer) throw new Error('Slide disclosure exceeds editable margin');
    addText(slide, footer.text, box, footer.size, '#626D78');
  }
}

function addSlide(presentation, plan, spec, suffix = '') {
  const slide = presentation.slides.add();
  slide.background.fill = '#FFFFFF';
  const titleBox = {left: 68, top: 42, width: 1144, height: 134};
  const title = fitText((spec.title + suffix).replace(/\s+/gu, ' '), titleBox.width, titleBox.height, 44, 30);
  if (!title) throw new Error('Slide title exceeds editable text capacity');
  addText(slide, title.text, titleBox, title.size, NAVY, true);
  slide.speakerNotes.text = [
    `Knowledge: ${spec.unit_ids.join(', ')}`,
    `Sources: ${spec.citation || spec.source_ids.join(', ')}`,
    `Reading scope: ${plan.scope}`,
    spec.bullets.join('\n'),
    (spec.figure_limitations || []).join('\n'),
    spec.missing_topics.length ? `Missing evidence: ${spec.missing_topics.join(', ')}` : '',
  ].filter(Boolean).join('\n');
  return slide;
}

function addDiagram(presentation, plan, spec) {
  const slide = addSlide(presentation, plan, spec);
  const nodes = spec.diagram.nodes;
  const levels = nodes.map(() => 0);
  for (let pass = 0; pass < nodes.length; pass++) {
    for (const [from, to] of spec.diagram.edges) levels[to] = Math.max(levels[to], levels[from] + 1);
  }
  if (Math.max(...levels) >= nodes.length) {
    levels.splice(0, levels.length, ...nodes.map((_, index) => index % 3));
  }
  const depth = Math.max(...levels) + 1;
  const fitGraph = vertical => nodes.map((label, index) => {
    const layer = levels[index];
    const siblings = levels.map((level, number) => level === layer ? number : -1).filter(number => number >= 0);
    const sibling = siblings.indexOf(index);
    const across = vertical ? 1120 / siblings.length : 1120 / depth;
    const down = vertical ? 420 / depth : 420 / siblings.length;
    const width = Math.min(vertical ? 780 : 540, across - 24);
    const height = Math.min(120, down - 14);
    const position = {left: 80 + across * (vertical ? sibling : layer) + (across - width) / 2,
      top: 195 + down * (vertical ? layer : sibling) + (down - height) / 2, width, height};
    return {position, label: fitText(label.trim().replace(/\s+/gu, ' '), width - 16, height - 10, 27, 18)};
  });
  let vertical = depth > 4;
  let layout = fitGraph(vertical);
  if (layout.some(node => !node.label)) {
    vertical = !vertical;
    layout = fitGraph(vertical);
  }
  if (layout.some(node => !node.label)) throw new Error('Diagram labels exceed editable node capacity');
  const boxes = layout.map(({position, label}, index) => {
    const shape = slide.shapes.add({
      geometry: 'roundRect', name: `method-node-${index}`,
      position,
      fill: '#EFF3F7', line: { fill: NAVY, width: 1.5 },
    });
    shape.text = label.text;
    shape.text.style = { typeface: 'Times New Roman', fontSize: label.size, color: NAVY,
      alignment: 'center', verticalAlignment: 'middle', insets: 5, wrap: 'none' };
    return shape;
  });
  for (const [from, to] of spec.diagram.edges) {
    const start = layout[from].position;
    const end = layout[to].position;
    const useVertical = levels[from] === levels[to] ? !vertical : vertical;
    const sides = useVertical
      ? (end.top > start.top ? ['bottom', 'top'] : ['top', 'bottom'])
      : (end.left > start.left ? ['right', 'left'] : ['left', 'right']);
    slide.shapes.connect(boxes[from], boxes[to], {
      fromSide: sides[0], toSide: sides[1],
      kind: 'elbow', line: { fill: NAVY, width: 2 },
      tail: { type: 'arrow', width: 'med', length: 'med' },
    });
  }
  addText(slide, plan.language === 'en' ? 'Diagram based on the available method description' : '根据已读取的方法说明整理',
    { left: 68, top: 648, width: 1144, height: 30 }, 20, '#626D78');
  addDisclosure(slide, plan, spec);
}

async function addOriginalFigure(presentation, plan, spec, image) {
  const slide = addSlide(presentation, plan, spec);
  slide.images.add({ blob: await fs.readFile(image.path), contentType: 'image/png',
    alt: image.label, fit: 'contain',
    position: { left: 68, top: 174, width: 1144, height: 450 } });
  const captionBox = { left: 68, top: 630, width: 1144, height: 72 };
  const caption = fitText(image.label.replace(/\s+/gu, ' '), captionBox.width, captionBox.height, 22, 18);
  if (!caption) throw new Error('Figure label exceeds editable caption capacity');
  addText(slide, caption.text, captionBox, caption.size, '#626D78');
  slide.speakerNotes.text = [image.label, image.caption, spec.citation, plan.scope]
    .filter(Boolean).join('\n');
}

function coverDetails(spec, plan) {
  const english = plan.language === 'en';
  const labels = {authors: english ? 'Authors: ' : '作者：',
    venue: english ? 'Publication venue: ' : '出版出处：',
    preprint: english ? 'Preprint: ' : '预印本：'};
  const bibliography = plan.bibliography || {};
  const metadata = Object.entries(labels).flatMap(([key, label]) =>
    bibliography[key]?.text ? [label + bibliography[key].text] : []);
  const normalized = text => text.replace(/[\p{P}\s]+/gu, '').toLowerCase();
  const other = spec.bullets.filter(text => !text.includes(spec.title) &&
    !Object.values(bibliography).some(field => field.text && normalized(text).includes(normalized(field.text))));
  return [...metadata, ...other];
}

export async function renderPlan(plan, options) {
  const require = createRequire(path.join(path.resolve(options.modules), '../package.json'));
  const { Presentation, PresentationFile } = await import(pathToFileURL(require.resolve('@oai/artifact-tool')).href);
  const presentation = Presentation.create({ slideSize: { width: WIDTH, height: HEIGHT } });
  for (const spec of plan.slides) {
    if (spec.topics.length === 1 && spec.topics[0] === 'title') {
      const slide = presentation.slides.add();
      slide.background.fill = '#FFFFFF';
      const titleBox = { left: 90, top: 145, width: 1100, height: 182 };
      const heading = fitText(spec.title.replace(/\s+/gu, ' '), titleBox.width, titleBox.height, 44, 25);
      if (!heading) throw new Error('Cover title exceeds editable text capacity');
      addText(slide, heading.text, titleBox, heading.size, NAVY, true, 'center');
      const details = coverDetails(spec, plan);
      const options = {left: 110, width: 1060, height: 330, continuationHeight: 490,
        size: 25, gap: 18, color: NAVY, alignment: 'center'};
      const [first, ...continued] = textPages(details, options);
      addTextGroups(slide, first, {...options, top: 344});
      addDisclosure(slide, plan, spec);
      slide.speakerNotes.text = [details.join('\n'), spec.citation || spec.source_ids.join(', '), plan.scope,
        plan.bibliography && Object.keys(plan.bibliography).length ? JSON.stringify(plan.bibliography) : ''].filter(Boolean).join('\n');
      for (const [index, page] of continued.entries()) {
        const information = {...spec, title: plan.language === 'en' ? 'Publication details' : '论文出版信息'};
        const continuation = addSlide(presentation, plan, information, index ? ` (${index + 1})` : '');
        addTextGroups(continuation, page, {...options, top: 185});
        addDisclosure(continuation, plan, spec);
      }
      continue;
    }
    const originalOverview = spec.overview_display === 'original'
      ? spec.images.find(image => image.purpose === 'method_overview') : null;
    const showDiagram = spec.diagram && !originalOverview;
    if (originalOverview) await addOriginalFigure(presentation, plan, spec, originalOverview);
    else if (showDiagram) addDiagram(presentation, plan, spec);
    const pages = textPages(spec.bullets);
    for (const [index, groups] of pages.entries()) {
      if ((showDiagram || originalOverview) && !spec.bullets.length) continue;
      const suffix = index ? ` (${index + 1})` : '';
      const slide = addSlide(presentation, plan, spec, suffix);
      addTextGroups(slide, groups);
      addDisclosure(slide, plan, spec);
    }
    for (const image of spec.images || []) {
      if (image !== originalOverview) await addOriginalFigure(presentation, plan, spec, image);
    }
  }
  await fs.mkdir(options.output, { recursive: true });
  for (const [index, slide] of presentation.slides.items.entries()) {
    const png = await presentation.export({ slide, format: 'png', scale: 1 });
    await fs.writeFile(path.join(options.output, `slide-${String(index + 1).padStart(2, '0')}.png`),
      new Uint8Array(await png.arrayBuffer()));
  }
  const pptx = await PresentationFile.exportPptx(presentation);
  await pptx.save(path.join(options.output, 'presentation.pptx'));
  const receipt = { slides: presentation.slides.items.length,
    planned_slides: plan.slides.length, slide_size: [WIDTH, HEIGHT] };
  await fs.writeFile(path.join(options.output, 'render.json'), JSON.stringify(receipt, null, 2));
  return receipt;
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  const [input, output, modules] = process.argv.slice(2);
  renderPlan(JSON.parse(await fs.readFile(input, 'utf8')), { output, modules }).then(
    receipt => process.stdout.write(JSON.stringify(receipt) + '\n'),
    error => { process.stderr.write(String(error.stack || error) + '\n'); process.exitCode = 1; },
  );
}
