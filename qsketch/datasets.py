import torch
import copy


class ModulesDataset:
    """A dataset of torch modules.
    module_class: a torch.nn.Module
        this is the class name to turn into a dataset.
    device: string
        the device on which to generate the modules. Be careful that two
        ModulesDataset objects are equal if and only if they are using the same
        device.
    recycle: boolean
        whether or not to use recycling, which significantly accelerates
        accessing elements. If recycling is activated, a current element
        is kept in memory, which is reassigned new values on demand, instead
        of a new allocation.
    kwargs: dict
        parameters to provide to the class constructor when creating new
        elements
    """

    def __init__(self, module_class, device='cpu', recycle=True, **kwargs):
        self.module_class = module_class
        self.parameters = kwargs
        self.pos = 0
        self.current = None
        self.device = device
        self.recycle = recycle

    def __iter__(self):
        return self

    def __next__(self):
        self.pos += 1
        return self[self.pos]

    def recycle_module(self, module, index):
        """ default recycling method for modules.
        We need to make sure all recycling are performed with the same
        random sequence, which means it must be done on the same device.
        self.device is used here for this reason."""
        params = module.state_dict()
        torch.manual_seed(index)
        if (
                hasattr(module, 'reset_parameters')
                and callable(module.reset_parameters)):
            # we have a reset_parameters function. we need to call it after
            # being sure the module is on the right device
            module = module.to(self.device)
            module.reset_parameters()
        else:
            params = module.state_dict()
            for key in params:
                params[key] = torch.randn(
                    params[key].shape,
                    device=self.device)
            module.load_state_dict(params)

    def __getitem__(self, indexes):
        # get items, possibly using recycling
        if isinstance(indexes, int):
            if self.recycle:
                # only keeping track of a `current`
                # for singleton queries, not for list
                if self.current is None:
                    self.current = self.module_class(**self.parameters)
                result = self.current.to(self.device)
            else:
                result = self.module_class(**self.parameters).to(self.device)
            self.recycle_module(result, indexes)
            return result
        else:
            result = []
            for index in indexes:
                new_module = (self.module_class(**self.parameters)
                              .to(self.device))
                ModulesDataset.recycle_module(new_module, index)
                result += [new_module]
            return result

    def __call__(self, indexes):
        # get items, without recycling (new items necessarily)
        recycle_state = self.recycle
        self.recycle = False
        result = self[indexes]
        self.recycle = recycle_state
        return result


class TransformedDataset:
    """ Create a dataset whose items are obtained by applying a specified
    torch Module to the items and the targets of some other dataset."""

    def __init__(self, dataset, transform=None, target_transform=None,
                 device='cpu'):
        self.dataset = dataset
        # we move the transforms to cpu, because it's possible that this
        # dataset will be used for some datastream. In that case, the
        # dataset needs to be pickable, which will not happen if the
        # transform is on GPU. We will move to gpu if desired only in
        # the getitem method, assuming that we are in the right process
        # at this stage.
        # IMPORTANT: If you are using a TransformedDataset in multiprocessing,
        # and with a datastream, be extra-sure NOT to access its element before
        # running the stream.
        if transform is not None:
            transform = copy.deepcopy(transform).to('cpu')
        if target_transform is not None:
            target_transform = copy.deepcopy(target_transform).to('cpu')
        self.transform = transform
        self.target_transform = target_transform
        self.device = device

    def __getitem__(self, indices):
        if self.transform is not None:
            self.transform.to(self.device)
        if self.target_transform is not None:
            self.target_transform.to(self.device)
        with torch.no_grad():
            try:
                _ = iter(indices)
                iterable = True
            except TypeError as te:
                indices = [indices]
                iterable = False
            result = []
            for id in indices:
                (X, y) = self.dataset[id]
                if isinstance(X, torch.Tensor):
                    X = X.to(self.device)
                if isinstance(y, torch.Tensor):
                    y = y.to(self.device)
                result += [
                 (X if self.transform is None else self.transform(X),
                  (y if self.target_transform is None
                   else self.target_transform(y)))
                ]
            return result[0] if not iterable else result

    def __len__(self):
        return len(self.dataset)