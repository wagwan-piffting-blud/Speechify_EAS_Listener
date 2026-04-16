/* CART Tree SVG Visualizer (f0tr + durt) */

let currentTreeType = 'f0tr';

async function loadTrees() {
    window._treesLoaded = true;

    // Populate durt phone selector
    const sel = document.getElementById('durt-phone-select');
    const phones = ["aa","ae","ah","ao","aw","ax","ay","b","ch","dx","d","dh",
                    "eh","el","er","en","ey","f","g","hh","ih","ix","iy","jh",
                    "k","l","m","n","ng","ow","oy","p","pau","r","s","sh","t",
                    "th","uh","uw","v","w","xx","y","z","zh"];
    phones.forEach(ph => {
        const opt = document.createElement('option');
        opt.value = ph;
        opt.textContent = ph;
        sel.appendChild(opt);
    });

    sel.addEventListener('change', () => {
        if (sel.value) loadDurtTree(sel.value);
    });

    // Tree type tabs
    document.querySelectorAll('.tree-tab').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('.tree-tab').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            currentTreeType = btn.dataset.tree;
            const phoneSel = document.getElementById('durt-phone-select');
            phoneSel.style.display = currentTreeType === 'durt' ? 'inline-block' : 'none';
            if (currentTreeType === 'f0tr') loadF0trTree();
            else if (phoneSel.value) loadDurtTree(phoneSel.value);
        });
    });

    loadF0trTree();
}

async function loadF0trTree() {
    const data = await apiJson('/api/vin/f0tr');
    if (!data) return;

    const leaves = data.nodes.filter(n => n.is_leaf);
    const branches = data.nodes.filter(n => !n.is_leaf);
    const means = leaves.map(l => l.mean);
    const minMean = Math.min(...means);
    const maxMean = Math.max(...means);

    document.getElementById('tree-info').innerHTML =
        `f0tr: ${data.nodes.length} nodes (${branches.length} branches, ${leaves.length} leaves) | ` +
        `Range: ${minMean.toFixed(1)} - ${maxMean.toFixed(1)} Hz | ` +
        `Labels: ${data.labels.length} | Questions: ${data.questions.length}`;

    renderTree(data, 'f0tr');
}

async function loadDurtTree(phone) {
    const data = await apiJson(`/api/vin/durt/${phone}`);
    if (!data) return;

    const leaves = data.nodes.filter(n => n.is_leaf);
    const branches = data.nodes.filter(n => !n.is_leaf);
    const means = leaves.map(l => l.mean);

    document.getElementById('tree-info').innerHTML =
        `durt/${phone}: ${data.nodes.length} nodes (${branches.length} branches, ${leaves.length} leaves) | ` +
        `Duration range: ${Math.min(...means).toFixed(1)} - ${Math.max(...means).toFixed(1)} (x0.5ms)`;

    renderTree({nodes: data.nodes, labels: [], questions: []}, 'durt');
}

function renderTree(data, type) {
    const container = document.getElementById('tree-container');

    if (!data.nodes || data.nodes.length === 0) {
        container.innerHTML = '<p class="placeholder">No tree data available.</p>';
        return;
    }

    // Build tree structure: map node_index -> node
    const nodeMap = {};
    data.nodes.forEach(n => { nodeMap[n.node_index] = n; });

    // Find root (node_index 0 or first node)
    const root = nodeMap[0] || data.nodes[0];

    // Layout: compute positions using simple recursive layout
    const NODE_W = 28;
    const NODE_H = 24;
    const LEVEL_H = 56;

    // Count leaves under each node for proportional spacing
    function countLeaves(node) {
        if (!node) return 0;
        if (node.is_leaf) return 1;
        const left = nodeMap[node.yes_child];
        const right = nodeMap[node.no_child];
        return countLeaves(left) + countLeaves(right);
    }

    function layoutNode(node, depth, xMin, xMax) {
        if (!node) return null;
        const x = (xMin + xMax) / 2;
        const y = depth * LEVEL_H + 30;
        const result = { node, x, y, children: [] };

        if (!node.is_leaf) {
            const left = nodeMap[node.yes_child];
            const right = nodeMap[node.no_child];
            const lCount = countLeaves(left) || 1;
            const rCount = countLeaves(right) || 1;
            // Split space proportional to leaf count
            const split = xMin + (xMax - xMin) * (lCount / (lCount + rCount));
            if (left) result.children.push(layoutNode(left, depth + 1, xMin, split));
            if (right) result.children.push(layoutNode(right, depth + 1, split, xMax));
        }
        return result;
    }

    // Compute depth for width estimation
    function maxDepth(node, d) {
        if (!node || node.is_leaf) return d;
        const left = nodeMap[node.yes_child];
        const right = nodeMap[node.no_child];
        return Math.max(
            left ? maxDepth(left, d + 1) : d,
            right ? maxDepth(right, d + 1) : d
        );
    }

    const depth = maxDepth(root, 0);
    const leafCount = data.nodes.filter(n => n.is_leaf).length;
    const width = Math.max(400, leafCount * 65);
    const height = (depth + 1) * LEVEL_H + 40;

    const layoutRoot = layoutNode(root, 0, 0, width);

    // Get leaf means for color scale
    const leaves = data.nodes.filter(n => n.is_leaf);
    const means = leaves.map(l => l.mean);
    const minM = Math.min(...means);
    const maxM = Math.max(...means);

    function meanColor(m) {
        const t = maxM > minM ? (m - minM) / (maxM - minM) : 0.5;
        const h = (1 - t) * 240; // blue (low) to red (high)
        return `hsl(${h}, 70%, 50%)`;
    }

    // Render SVG
    let svg = `<svg width="${width}" height="${height}" viewBox="0 0 ${width} ${height}">`;

    function renderNode(layout) {
        if (!layout) return;
        const { node, x, y, children } = layout;

        // Draw edges to children
        children.forEach(child => {
            if (child) {
                svg += `<line x1="${x}" y1="${y + 12}" x2="${child.x}" y2="${child.y - 12}"
                         stroke="#3a3a44" stroke-width="1"/>`;
            }
        });

        // Draw node
        if (node.is_leaf) {
            const color = meanColor(node.mean);
            const label = type === 'f0tr'
                ? `${node.mean.toFixed(1)}`
                : `${(node.mean * 0.5).toFixed(1)}`;
            const unit = type === 'f0tr' ? 'Hz' : 'ms';
            svg += `<rect x="${x-26}" y="${y-10}" width="52" height="20" rx="3"
                     fill="${color}" opacity="0.85"/>`;
            svg += `<text x="${x}" y="${y+3}" text-anchor="middle" fill="white" font-size="9">${label} ${unit}</text>`;
        } else {
            const qIdx = node.question_idx;
            const qText = data.questions[qIdx] || `Q${qIdx}`;
            const shortQ = qText.length > 10 ? qText.substring(0, 10) + '..' : qText;
            svg += `<circle cx="${x}" cy="${y}" r="12" fill="#2e2e38" stroke="#8aacb8" stroke-width="1.2"/>`;
            svg += `<text x="${x}" y="${y+3}" text-anchor="middle" fill="#8aacb8" font-size="7">${shortQ}</text>`;
        }

        children.forEach(renderNode);
    }

    renderNode(layoutRoot);
    svg += '</svg>';
    container.innerHTML = svg;

    // Make it zoomable + pannable
    initTreePanZoom(container.querySelector('svg'));
}

function initTreePanZoom(svg) {
    if (!svg) return;

    // Use viewBox manipulation for smooth 1:1 pixel pan/zoom
    svg.style.width = '100%';
    svg.style.height = '100%';
    svg.style.cursor = 'grab';
    svg.removeAttribute('width');
    svg.removeAttribute('height');

    // Get the natural content bounds
    const origW = parseFloat(svg.getAttribute('viewBox')?.split(' ')[2] || 800);
    const origH = parseFloat(svg.getAttribute('viewBox')?.split(' ')[3] || 600);

    // viewBox state
    let vbX = 0, vbY = 0, vbW = origW, vbH = origH;
    let dragging = false;
    let dragStartX = 0, dragStartY = 0;
    let dragVbX = 0, dragVbY = 0;

    function applyViewBox() {
        svg.setAttribute('viewBox', `${vbX} ${vbY} ${vbW} ${vbH}`);
    }

    // Wheel zoom toward mouse
    svg.addEventListener('wheel', (e) => {
        e.preventDefault();
        const rect = svg.getBoundingClientRect();
        // Mouse position as fraction of SVG element
        const fx = (e.clientX - rect.left) / rect.width;
        const fy = (e.clientY - rect.top) / rect.height;
        // Mouse position in viewBox coords
        const mx = vbX + fx * vbW;
        const my = vbY + fy * vbH;

        const factor = e.deltaY > 0 ? 1.15 : 0.87;
        const newW = Math.max(20, Math.min(origW * 20, vbW * factor));
        const newH = Math.max(15, Math.min(origH * 20, vbH * factor));

        // Zoom centered on mouse
        vbX = mx - fx * newW;
        vbY = my - fy * newH;
        vbW = newW;
        vbH = newH;

        applyViewBox();
    }, {passive: false});

    // Pan via drag (any mouse button)
    svg.addEventListener('mousedown', (e) => {
        dragging = true;
        dragStartX = e.clientX;
        dragStartY = e.clientY;
        dragVbX = vbX;
        dragVbY = vbY;
        svg.style.cursor = 'grabbing';
        e.preventDefault();
    });

    window.addEventListener('mousemove', (e) => {
        if (!dragging) return;
        const rect = svg.getBoundingClientRect();
        // Convert pixel delta to viewBox units
        const dx = (e.clientX - dragStartX) * (vbW / rect.width);
        const dy = (e.clientY - dragStartY) * (vbH / rect.height);
        vbX = dragVbX - dx;
        vbY = dragVbY - dy;
        applyViewBox();
    });

    window.addEventListener('mouseup', () => {
        if (dragging) {
            dragging = false;
            svg.style.cursor = 'grab';
        }
    });

    // Double-click to fit-all
    svg.addEventListener('dblclick', () => {
        vbX = 0; vbY = 0; vbW = origW; vbH = origH;
        applyViewBox();
    });
}
