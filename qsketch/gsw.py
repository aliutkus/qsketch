import torch
import queue
from .datastream import DataStream
from .datasets import ModulesDataset
from .sketch import Sketcher, sketch
from torchsearchsorted import searchsorted


# A class for random normalized linear projections, which is the module under
class LinearProjector(torch.nn.Linear):
    def __init__(self, input_shape, num_projections):
        self.dim_in = torch.prod(torch.tensor(input_shape))
        try:
            _ = iter(num_projections)
            self.shape_out = num_projections
        except TypeError as te:
            self.shape_out = [num_projections, ]
        self.dim_out = torch.prod(torch.tensor(self.shape_out))
        super(LinearProjector, self).__init__(
            in_features=self.dim_in,
            out_features=self.dim_out,
            bias=False)
        self.reset_parameters()

    def forward(self, input):
        input = input.view(input.shape[0], -1)
        result = super(LinearProjector, self).forward(
            input)
        result = result.view(-1, *self.shape_out)
        return result

    def reset_parameters(self):
        super(LinearProjector, self).reset_parameters()
        new_weight = self.weight

        # make sure each projector is normalized
        self.weight = torch.nn.Parameter(
           new_weight/torch.norm(new_weight, dim=1, keepdim=True))

    def eval(self):
        self.weight.requires_grad = False

    def train(self):
        self.weight.requires_grad = True

    def backward(self, grad):
        """Manually compute the gradient of the layer for any input"""
        return torch.mm(grad.view(grad.shape[0], -1), self.weight)


def sw(batch1, batch2, num_projections=1000):
    """directly compute the sliced Wasserstein distance between two
    batches of samples. This is done by randomly picking random projections,
    sketching the batches with them, and compute the squared error between
    the batches. The number of percentiles taken is the smallest of the two
    number of samples.

    batch1: Tensor, (num_samples,) + shape
    batch2: Tensor, (num_samples,) + shape
    """

    # check that dimensions match
    if batch1.shape[1:] != batch2.shape[1:]:
        raise Exception('sw: except the first one, dimension of the batches '
                        'must match.')
    projectors = LinearProjector(input_shape=batch1.shape[1:],
                                 num_projections=num_projections)

    # pick the smallest of the two number of samples as the number of quantiles
    num_percentiles = min(batch1.shape[0], batch2.shape[0])
    percentiles = torch.linspace(0, 100, num_percentiles)

    # compute the percentiles on the two batches_features
    sketch1 = sketch(projectors, batch1, percentiles)
    sketch2 = sketch(projectors, batch2, percentiles)

    # return SW as the sum of the squared error between them
    return torch.nn.MSELoss(reduction='sum')(sketch1, sketch2)


class GSW:
    """Generalized sliced Wasserstein.
    A class for computing the sliced wasserstein distance of a batch
    to a dataset.
    The `generalized` part comes from the fact that any ModulesDataset may
    be used to compute the projections. By default, random linear projections
    are used through a ModulesDataset of LinearProjector, leading to the
    classical sliced Wasserstein distance.
    """

    def __init__(self, dataset,
                 num_percentiles=500,
                 num_examples=5000,
                 projectors=5000,
                 batchsize=1,
                 manual_refresh=False,
                 asynchronous=True,
                 device='cpu',
                 num_workers_data=2,
                 num_sketchers=2):
        """Create a GSW object.

        Parameters:
        -----------

        dataset: Dataset object
            the object which contains the data against which we will compute
            the GSW distance
        num_percentiles: int
            the number of percentiles to compute on the dataset to perform
            comparison. If this is too high for a particular batch, then only
            a subset of those will be used for computing the GSW,
        projectors: {int | dataset of torch Modules (such as a
                    ModulesDataset object)}
            specifies the projectors that will be used. If it is an int,
            then a new ModulesDataset object will be created with the
            LinearProjector class and this number of projections,
            leading to the sliced Wasserstein distance. If it is a dataset
            of torch Module objects, will be used directly.
        batchsize: int [scalar]
            The cost is obtained by accumulating the result over `batchsize`
            different random projectors. This is useful in the case too many
            projections are desired that cannot be computed in a single shot.
        manual_refresh: boolean
            if False (default), then the `batchsize` projectors to use for
            sketching will be automatically drawn anew at each call, yielding
            different target quantiles each time.
            If True, the user must explicitly call the `udpate` function
            whenever new targets are desired
        asynchronous: boolean
            if True, will internally start a stream, meaning that the dataset
            will continuously be sketched in a parallel fashion. This makes
            computation of reference quantiles faster. If False, reference
            quantiles will be computed on demand
        device: 'cpu' or 'cuda'
            the device on which to perform the sketching
        num_workers_data: int
            the number of workers to use for the DataStream (to get data from
            the dataset)
        num_sketchers: int
            the number of workers to use for computing sketches"""
        self.datastream = DataStream(dataset, device=device,
                                     num_workers=num_workers_data)
        self.datastream.stream()
        self.num_percentiles = num_percentiles
        self.percentiles = torch.linspace(0, 100, num_percentiles)
        self.sketcher = Sketcher(data_source=self.datastream,
                                 percentiles=self.percentiles,
                                 num_examples=num_examples)
        if isinstance(projectors, int):
            # trying to access the first item from the dataset to identify the
            # shape automatically. We support the case where an item is a
            # collection of torch.Tensor, or a collection of (X, y) tuples,
            # where X are samples from the distribution we are interested in
            first_item = dataset[0]
            if not isinstance(first_item, torch.Tensor):
                first_item = first_item[0]
            data_shape = first_item.shape
            self.projectors = ModulesDataset(
                                    module_class=LinearProjector,
                                    device=device,
                                    input_shape=data_shape,
                                    num_projections=projectors)
        else:
            self.projectors = projectors
        self.asynchronous = asynchronous
        if asynchronous:
            self.sketcher.stream(modules=self.projectors,
                                 num_sketches=-1,
                                 num_epochs=1,
                                 num_workers=num_sketchers)
        self.target_percentiles = None
        self.projector_ids = None
        self.manual_refresh = manual_refresh
        self.batchsize = batchsize
        self.device = device

    def refresh(self):
        """refreshes the targets of the GSW object

        This function draws anew the projections to use for the computation
        of the GSW cost, and computes the associated target percentiles on the
        data.
        """
        if self.asynchronous:
            self.target_percentiles = []
            self.projector_ids = []
            for item in range(self.batchsize):
                    (target_percentiles,
                     projector_id) = self.sketcher.queue.get()
                    self.target_percentiles += [target_percentiles, ]
                    self.projector_ids += [projector_id, ]
        else:
            self.projector_ids = torch.randint(low=0,
                                               high=len(self.projectors),
                                               size=(self.batchsize,))
            # avoiding to put all the projectors in memory, calling one by one
            self.target_percentiles = [self.sketcher(self.projectors[id])
                                       for id in self.projector_ids]

    def __call__(self, batch):
        """"compute the (generalized) sliced Wasserstein distance between
        the object dataset and the provided batch

        batch: torch.Tensor (num_samples, ) + sample_shape
            the batch of samples for which to compute the GSW distance to
            the dataset."""
        # update the target if required
        if (self.target_percentiles is None) or not self.manual_refresh:
            self.refresh()

        # bringing the target percentiles to the batch device (if not done)
        # already
        self.target_percentiles = [t.to(batch.device) for t in
                                   self.target_percentiles]

        # if the batch is too small, we may have to reduce the number of
        # percentiles
        num_percentiles = min(batch.shape[0], self.num_percentiles)
        if num_percentiles != self.num_percentiles:
            indices = searchsorted(
                        self.percentiles[None, :],
                        torch.linspace(0, 100, num_percentiles)[None, :]
                        ).long()
        else:
            indices = Ellipsis
        percentiles = self.percentiles[indices].squeeze()

        loss = torch.tensor(0, device=batch.device)
        for (projector_id, target_percentiles) in zip(
                                    self.projector_ids,
                                    self.target_percentiles):
            # get the projector
            projector = self.projectors[projector_id]
            test_percentiles = sketch(projector, batch, percentiles)
            loss = loss + torch.nn.MSELoss()(
                target_percentiles[indices].squeeze(),
                test_percentiles.to(batch.device))

        loss = loss / self.batchsize
        return loss
