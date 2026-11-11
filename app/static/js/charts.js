function renderHistoryChart(chartData) {
    const ctx = document.getElementById('chart-history');
    if (!ctx) return;
    new Chart(ctx, {
        type: 'bar',
        data: {
            labels: chartData.labels,
            datasets: [
                {
                    label: 'Revenus',
                    data: chartData.incomes,
                    backgroundColor: 'rgba(91, 141, 217, 0.7)',
                },
                {
                    label: 'Dépenses',
                    data: chartData.spendings,
                    backgroundColor: 'rgba(224, 123, 57, 0.7)',
                },
            ]
        },
        options: {
            responsive: true,
            plugins: { legend: { position: 'top' } },
            scales: { y: { beginAtZero: true } }
        }
    });
}

function renderCategoryChart(canvasId, allocated, idealData, realData, labels) {
    const ctx = document.getElementById(canvasId);
    if (!ctx) return;
    new Chart(ctx, {
        type: 'line',
        data: {
            labels: labels,
            datasets: [
                {
                    label: 'Budget idéal',
                    data: idealData,
                    borderColor: 'rgba(91, 141, 217, 1)',
                    borderDash: [5, 5],
                    fill: false,
                    tension: 0,
                },
                {
                    label: 'Dépenses réelles',
                    data: realData,
                    borderColor: 'rgba(224, 123, 57, 1)',
                    fill: false,
                    tension: 0,
                },
            ]
        },
        options: { responsive: true, plugins: { legend: { position: 'top' } } }
    });
}
